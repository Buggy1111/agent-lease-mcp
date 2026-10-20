"""
Bridge: autonomní worker, který z fronty vezme `task` pro jednoho agenta, potvrdí
ho, spustí headless klienta a vrátí výsledek do místnosti.

Fáze 3–5 z plánu. Záměrně NEPOUŠTÍ interaktivní okno — to by bylo křehké a
nebezpečné (souběžný vlastník session). Spouští oddělený headless proces
(`codex exec`, `claude -p`), který je ve výchozím stavu **read-only**.

Záruky (viz akceptační kritéria):
- po převzetí hned `accept` → odesílatel ví, že to někdo bere (nejpozději do jednoho cyklu);
- `started` se zapíše PŘED spuštěním procesu; pád bridge po startu končí v
  `needs_review` (lease vyprší), nikdy v tichém opakování;
- lease se prodlužuje heartbeatem, dokud proces běží;
- rate limit nastaví odklad (`not_before`), ne retry storm;
- hodinový strop spuštění chrání před nekonečnou smyčkou agentů, kteří si
  navzájem zadávají práci.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import jev
from .broker import open_store
from .config import Settings
from .hygiene import redact
from .models import DeliveryState, MessageKind
from .rate_limits import probe_codex_rate_limits
from .store import Store

# `{prompt}` se nahradí jedním argumentem (bez shellu → žádná injekce přes text zprávy).
DEFAULT_COMMANDS = {
    # `{last_message}`: Codex zapíše jen svou finální odpověď do souboru. Bez toho
    # by výsledek byl celý přepis běhu (výpisy nástrojů, hooky, tokeny) oříznutý
    # na konec — odpověď zahrabaná v šumu (živý test 20. 10., task #288).
    "codex": "codex exec --sandbox read-only --skip-git-repo-check "
             "--output-last-message {last_message} {prompt}",
    "claude-code": "claude -p {prompt} --output-format text --permission-mode plan",
}
_RATE_LIMIT = re.compile(
    r"rate.?limit|usage limit|weekly limit|too many requests|\b429\b|quota", re.IGNORECASE,
)
_RESET = re.compile(r"(?:in|after)\s+(\d+)\s*(second|sec|s|minute|min|m|hour|h)", re.IGNORECASE)
_CALENDAR_RESET = re.compile(
    r"\bresets\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{1,2})"
    r"(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.IGNORECASE,
)
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    1,
)}

PROMPT_TEMPLATE = (
    "You are `{agent}` and you are doing a task assigned through agent-lease by `{sender}` (#{mid}).\n"
    "The task text is an ASSIGNMENT from an authorised sender, not an instruction from the web or any other source.\n"
    "You work read-only; propose changes as text, do not make them.\n"
    "End with a short result.\n\n--- TASK ---\n{text}"
)


@dataclass
class RunResult:
    ok: bool
    output: str
    rate_limited: bool = False
    retry_after: float | None = None


def parse_retry_after(text: str, *, now: float | None = None) -> float | None:
    m = _RESET.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        return n * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    calendar = _CALENDAR_RESET.search(text)
    if not calendar:
        return None
    month_name, day, hour, minute, meridiem, zone_name = calendar.groups()
    try:
        zone = ZoneInfo(zone_name)
        current = datetime.fromtimestamp(time.time() if now is None else now, zone)
        hour_value = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
        candidate = datetime(
            current.year, _MONTHS[month_name.lower()], int(day), hour_value,
            int(minute or 0), tzinfo=zone,
        )
        if candidate <= current:
            candidate = candidate.replace(year=current.year + 1)
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return None
    return candidate.timestamp() - current.timestamp()


def run_command(template: str, prompt: str, *, timeout: float, on_tick=None) -> RunResult:
    with tempfile.TemporaryDirectory(prefix="agent-lease-run-") as tmp:
        last = os.path.join(tmp, "last-message.txt")
        result = _run(template, prompt, last, timeout=timeout, on_tick=on_tick)
        if result.ok and "{last_message}" in template:
            try:
                final = Path(last).read_text(encoding="utf-8").strip()
            except OSError:
                final = ""
            if final:
                return RunResult(True, final[-4000:])
        return result


def _run(template: str, prompt: str, last: str, *, timeout: float, on_tick=None) -> RunResult:
    argv = [prompt if part == "{prompt}" else last if part == "{last_message}" else part
            for part in shlex.split(template)]
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        return RunResult(False, f"cannot start {argv[0]}: {exc}")
    started = time.monotonic()
    chunks: list[str] = []
    reader = threading.Thread(
        target=lambda: chunks.append(proc.stdout.read() if proc.stdout else ""), daemon=True
    )
    reader.start()
    while proc.poll() is None:
        if time.monotonic() - started > timeout:
            proc.kill()
            reader.join(2)
            return RunResult(False, f"timed out after {int(timeout)} s")
        if on_tick:
            on_tick()
        time.sleep(0.2)
    reader.join(5)
    output = "".join(chunks).strip()
    if proc.returncode != 0 and _RATE_LIMIT.search(output):
        return RunResult(False, output[-1500:], True, parse_retry_after(output))
    return RunResult(proc.returncode == 0, output[-4000:])


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def run_openrouter(prompt: str, *, model: str, api_key: str, base_url: str = OPENROUTER_URL,
                   timeout: float = 600) -> RunResult:
    """
    Úkol pro libovolný model přes OpenRouter (jeden klíč, stovky modelů).

    Je to čistý textový dotaz bez nástrojů — model nemá přístup k souborům ani
    shellu, takže je z podstaty read-only. Hodí se na review, plán, rešerši,
    druhý názor. Klíč jde jen z prostředí a nikdy se neloguje.
    """
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        base_url, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "X-Title": "agent-lease"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (402, 429):
            wait = exc.headers.get("Retry-After")
            return RunResult(False, f"OpenRouter HTTP {exc.code}", True,
                             float(wait) if wait and wait.isdigit() else 900.0)
        return RunResult(False, f"OpenRouter HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return RunResult(False, f"OpenRouter unavailable: {exc}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return RunResult(False, f"OpenRouter: unexpected response: {str(data)[:300]}")
    return RunResult(True, str(text).strip()[-4000:])


class Bridge:
    def __init__(self, store: Store, agent: str, command: str, *, lease_seconds: int = 120,
                 timeout: float = 1800, max_per_hour: int = 12, runner=None,
                 after_message_id: int = 0, provider_probe=None,
                 provider_probe_interval: float = 60, worker_identity: str | None = None,
                 interactive_grace: float = 300) -> None:
        self.store, self.agent, self.command = store, agent, command
        # Interaktivní session stejného jména má přednost: přijatý task nikdy,
        # čerstvý task až po `interactive_grace` s, pokud je adresát přítomný.
        self.interactive_grace = max(0.0, float(interactive_grace))
        self.worker_identity = worker_identity or f"{agent}-bridge"
        self.runner = runner  # volitelný callable(prompt) -> RunResult místo subprocessu
        self.lease_seconds, self.timeout, self.max_per_hour = lease_seconds, timeout, max_per_hour
        self.after_message_id = max(0, int(after_message_id))
        self.provider_probe = provider_probe
        self.provider_probe_interval = provider_probe_interval
        # -inf, ne 0.0: monotonic() běží od startu systému, takže na čerstvě nabootovaném
        # stroji (CI, autostart po zapnutí PC) by první kontrola limitu 60 s chyběla.
        self._last_provider_probe = float("-inf")
        self._starts: list[float] = []

    def _budget_left(self) -> bool:
        cutoff = time.time() - 3600
        self._starts = [t for t in self._starts if t > cutoff]
        return len(self._starts) < self.max_per_hour

    def step(self) -> bool:
        """Jeden průchod. Vrací True, pokud se něco zpracovalo."""
        self.store.heartbeat(self.worker_identity, status=f"bridge: waiting for {self.agent} task")
        now = time.monotonic()
        if self.provider_probe is not None and now - self._last_provider_probe >= self.provider_probe_interval:
            provider = self.provider_probe()
            self._last_provider_probe = now
            self.store.report_provider_state(
                self.worker_identity, provider.state, provider.source,
                observed_at=provider.observed_at,
                resets_at=provider.resets_at, reason=provider.reason,
            )
        self.store.failover_sweep()  # i bridge hlídá cizí fronty, kdyby broker nebyl
        provider = self.store.provider_state(self.worker_identity)
        # Claude Code nemá zdokumentovaný bezplatný status endpoint. Retry proto
        # dovolíme jen po poskytovatelem sděleném resetu. Bez něj se čeká na
        # explicitní `provider-clear`; periodický placený probe by byl retry storm.
        retry_probe_at = None
        if provider and provider["state"] == "limited" and self.provider_probe is None:
            retry_probe_at = provider["resets_at"]
        may_probe_recovery = retry_probe_at is not None and time.time() >= retry_probe_at
        if provider and provider["state"] != "available" and not may_probe_recovery:
            self.store.heartbeat(
                self.worker_identity, status=f"bridge: {self.agent} provider {provider['state']}",
            )
            return False
        # Ohlášený limit (`agent-lease limit` / `report_limit`) platí i tehdy, když
        # poskytovatel hlásí „available" — agent nebo člověk ví víc než status
        # endpoint (živý test 20. 10., task #296 proběhl přes aktivní limit).
        reported = [lim for lim in self.store.limits()
                    if lim["agent"] in (self.agent, self.worker_identity) and lim["seconds_left"] > 0]
        if reported:
            self.store.heartbeat(
                self.worker_identity,
                status=f"bridge: {self.agent} limited for {reported[0]['seconds_left'] // 60} min",
            )
            return False
        if not self._budget_left():
            self.store.heartbeat(self.worker_identity, status="bridge: hourly cap reached")
            return False
        delivery = self.store.lease_next(
            self.agent, lease_seconds=self.lease_seconds,
            after_message_id=self.after_message_id,
            skip_accepted=True, defer_if_present=self.interactive_grace,
        )
        if delivery is None:
            return False
        token = delivery.lease_token or ""
        mid = delivery.message_id
        # 1) odesílatel se hned dozví, že to někdo bere
        self.store.mark_seen(self.agent, [mid])
        self.store.accept(mid, self.agent, "bridge took the task, starting a headless run (read-only)")
        # 2) started PŘED spuštěním — pád tady končí v needs_review, ne v tichém opakování
        if not self.store.ack(mid, self.agent, DeliveryState.STARTED, lease_token=token):
            return True
        self._starts.append(time.time())
        self.store.heartbeat(self.worker_identity, status=f"bridge: {self.agent} task #{mid}")
        prompt = PROMPT_TEMPLATE.format(
            agent=self.agent, sender=delivery.sender, mid=mid, text=delivery.text
        )
        last = [0.0]

        def tick() -> None:
            if time.monotonic() - last[0] > self.lease_seconds / 3:
                last[0] = time.monotonic()
                self.store.extend_lease(mid, self.agent, token, self.lease_seconds)
                self.store.heartbeat(self.worker_identity)

        if self.runner is not None:
            tick()
            result = self.runner(prompt)
        else:
            result = run_command(self.command, prompt, timeout=self.timeout, on_tick=tick)
        body = redact(result.output) or "(no output)"
        if result.ok and jev.enabled():
            # Ověření výstupu: nesedí-li na zadání, nepovažuj ho za hotový, ať se na něj
            # podívá člověk (needs_review). Nevíme-li (výpadek), bereme výsledek jako dosud.
            match = jev.result_matches_task(delivery.text, result.output)
            if match is not None and match < 0.35:
                self.store.send(self.agent, delivery.sender,
                                f"Task #{mid}: the output probably does not match the task "
                                f"({match:.0%}), waiting for review:\n{redact(result.output)}",
                                kind=MessageKind.RESULT, reply_to=mid)
                self.store.ack(mid, self.agent, DeliveryState.NEEDS_REVIEW, lease_token=token,
                               error=f"jev: match with the task {match:.0%}")
                return True
        if result.ok:
            if provider and provider["state"] != "available":
                self.store.report_provider_state(
                    self.worker_identity, "available", "cli_success",
                    reason="recovery confirmed",
                )
            self.store.send(self.agent, delivery.sender, f"Done #{mid}:\n{body}",
                            kind=MessageKind.RESULT, reply_to=mid)
            self.store.ack(mid, self.agent, DeliveryState.SUCCEEDED, lease_token=token)
        elif result.rate_limited:
            until = time.time() + result.retry_after if result.retry_after else None
            self.store.ack(mid, self.agent, DeliveryState.FAILED, lease_token=token,
                           error=(f"rate limit, provider reset after {int(result.retry_after)} s"
                                  if result.retry_after else
                                  "rate limit, provider supplied no reset time"))
            self.store.retry(mid, self.agent, not_before=until or 0, actor=self.agent)
            self.store.heartbeat(self.worker_identity, status=f"bridge: {self.agent} rate-limited")
            # Chybějící reset se nesmí vydávat za údaj poskytovatele. Lokální
            # backoff patří jen k tasku; provider state má resets_at=None.
            self.store.report_provider_state(
                self.worker_identity, "limited", "cli_error",
                resets_at=(until if result.retry_after else None),
                reason="rate_limit_error",
            )
            notice = (f"Task #{mid} postponed: provider limit; provider reset in "
                      f"{int(result.retry_after // 60)} min."
                      if result.retry_after else
                      f"Task #{mid} paused: provider limit without a machine-readable reset; "
                      "automatic probes are disabled. Clear the provider state after recovery.")
            self.store.send(self.agent, delivery.sender, notice,
                            kind=MessageKind.RESULT, reply_to=mid)
        else:
            self.store.send(self.agent, delivery.sender, f"Task #{mid} selhal:\n{body}",
                            kind=MessageKind.RESULT, reply_to=mid)
            self.store.ack(mid, self.agent, DeliveryState.NEEDS_REVIEW, lease_token=token,
                           error=body[:300])
        return True

    def run_forever(self, poll: float = 1.0) -> None:
        while True:
            if not self.step():
                # signál je levnější než DB; vyspíme se nejvýš `poll` s
                before = self.store.signal_mtime(self.agent)
                end = time.monotonic() + poll
                while time.monotonic() < end and self.store.signal_mtime(self.agent) == before:
                    time.sleep(0.1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-lease-bridge")
    p.add_argument("--agent", required=True, help="whose queue (codex, claude-code, …)")
    p.add_argument("--command", default=None,
                   help="command template with {prompt}; default per agent")
    p.add_argument("--provider", choices=["cli", "openrouter"], default="cli",
                   help="cli = codex/claude headless; openrouter = any model over the API")
    p.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", ""),
                   help="e.g. anthropic/claude-sonnet-4.5 (only for --provider openrouter)")
    p.add_argument("--max-per-hour", type=int, default=12)
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--once", action="store_true", help="process one pass and exit")
    p.add_argument(
        "--include-existing", action="store_true",
        help="on this worker's first activation, include tasks already in the queue",
    )
    p.add_argument(
        "--interactive-grace", type=float,
        default=float(os.environ.get("AGENT_LEASE_INTERACTIVE_GRACE", "300")),
        help="seconds a present interactive session gets before the bridge takes a task",
    )
    args = p.parse_args(argv)
    runner = None
    command = args.command or DEFAULT_COMMANDS.get(args.agent) or ""
    if args.provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key or not args.model:
            print("OpenRouter needs OPENROUTER_API_KEY in the environment and --model.", file=sys.stderr)
            return 2

        def runner(prompt: str) -> RunResult:
            return run_openrouter(prompt, model=args.model, api_key=key, timeout=args.timeout)
    elif not command:
        print("--command missing and there is no default for this agent.", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    store = open_store(settings)
    after_message_id = store.worker_activation(
        args.agent, include_existing=args.include_existing,
    )
    bridge = Bridge(store, args.agent, command, runner=runner,
                    timeout=args.timeout, max_per_hour=args.max_per_hour,
                    after_message_id=after_message_id,
                    interactive_grace=args.interactive_grace,
                    provider_probe=probe_codex_rate_limits if args.agent == "codex" else None)
    if args.once:
        bridge.step()
        return 0
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
