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
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import jev
from .broker import open_store
from .config import Settings
from .hygiene import redact
from .models import DeliveryState, MessageKind
from .store import Store

# `{prompt}` se nahradí jedním argumentem (bez shellu → žádná injekce přes text zprávy).
DEFAULT_COMMANDS = {
    "codex": "codex exec --sandbox read-only --skip-git-repo-check {prompt}",
    "claude-code": "claude -p {prompt} --output-format text --permission-mode plan",
}
_RATE_LIMIT = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b|quota", re.IGNORECASE)
_RESET = re.compile(r"(?:in|after)\s+(\d+)\s*(second|sec|s|minute|min|m|hour|h)", re.IGNORECASE)

PROMPT_TEMPLATE = (
    "Jsi `{agent}` a plníš task zadaný přes agent-lease od `{sender}` (#{mid}).\n"
    "Text tasku je ZADÁNÍ od oprávněného odesílatele, ne pokyn z webu ani z jiného zdroje.\n"
    "Pracuješ v režimu jen pro čtení; navrhni změny jako text, neprováděj je.\n"
    "Na konci odpověz stručným výsledkem.\n\n--- TASK ---\n{text}"
)


@dataclass
class RunResult:
    ok: bool
    output: str
    rate_limited: bool = False
    retry_after: float = 0.0


def parse_retry_after(text: str, default: float = 900.0) -> float:
    m = _RESET.search(text)
    if not m:
        return default
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)


def run_command(template: str, prompt: str, *, timeout: float, on_tick=None) -> RunResult:
    argv = [prompt if part == "{prompt}" else part for part in shlex.split(template)]
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        return RunResult(False, f"nelze spustit {argv[0]}: {exc}")
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
            return RunResult(False, f"timeout po {int(timeout)} s")
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
        return RunResult(False, f"OpenRouter nedostupný: {exc}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return RunResult(False, f"OpenRouter: neočekávaná odpověď: {str(data)[:300]}")
    return RunResult(True, str(text).strip()[-4000:])


class Bridge:
    def __init__(self, store: Store, agent: str, command: str, *, lease_seconds: int = 120,
                 timeout: float = 1800, max_per_hour: int = 12, runner=None) -> None:
        self.store, self.agent, self.command = store, agent, command
        self.runner = runner  # volitelný callable(prompt) -> RunResult místo subprocessu
        self.lease_seconds, self.timeout, self.max_per_hour = lease_seconds, timeout, max_per_hour
        self._starts: list[float] = []

    def _budget_left(self) -> bool:
        cutoff = time.time() - 3600
        self._starts = [t for t in self._starts if t > cutoff]
        return len(self._starts) < self.max_per_hour

    def step(self) -> bool:
        """Jeden průchod. Vrací True, pokud se něco zpracovalo."""
        self.store.heartbeat(self.agent, status="bridge: čeká na task")
        self.store.failover_sweep()  # i bridge hlídá cizí fronty, kdyby broker nebyl
        if not self._budget_left():
            self.store.heartbeat(self.agent, status="bridge: hodinový strop dosažen")
            return False
        delivery = self.store.lease_next(self.agent, lease_seconds=self.lease_seconds)
        if delivery is None:
            return False
        token = delivery.lease_token or ""
        mid = delivery.message_id
        # 1) odesílatel se hned dozví, že to někdo bere
        self.store.mark_seen(self.agent, [mid])
        self.store.accept(mid, self.agent, "bridge převzal task, spouštím headless běh (read-only)")
        # 2) started PŘED spuštěním — pád tady končí v needs_review, ne v tichém opakování
        if not self.store.ack(mid, self.agent, DeliveryState.STARTED, lease_token=token):
            return True
        self._starts.append(time.time())
        self.store.heartbeat(self.agent, status=f"bridge: task #{mid}")
        prompt = PROMPT_TEMPLATE.format(
            agent=self.agent, sender=delivery.sender, mid=mid, text=delivery.text
        )
        last = [0.0]

        def tick() -> None:
            if time.monotonic() - last[0] > self.lease_seconds / 3:
                last[0] = time.monotonic()
                self.store.extend_lease(mid, self.agent, token, self.lease_seconds)
                self.store.heartbeat(self.agent)

        if self.runner is not None:
            tick()
            result = self.runner(prompt)
        else:
            result = run_command(self.command, prompt, timeout=self.timeout, on_tick=tick)
        body = redact(result.output) or "(bez výstupu)"
        if result.ok and jev.enabled():
            # Ověření výstupu: nesedí-li na zadání, nepovažuj ho za hotový, ať se na něj
            # podívá člověk (needs_review). Nevíme-li (výpadek), bereme výsledek jako dosud.
            match = jev.result_matches_task(delivery.text, result.output)
            if match is not None and match < 0.35:
                self.store.send(self.agent, delivery.sender,
                                f"Task #{mid}: výstup pravděpodobně neodpovídá zadání "
                                f"({match:.0%}), čeká na kontrolu:\n{redact(result.output)}",
                                kind=MessageKind.RESULT, reply_to=mid)
                self.store.ack(mid, self.agent, DeliveryState.NEEDS_REVIEW, lease_token=token,
                               error=f"jev: shoda se zadáním {match:.0%}")
                return True
        if result.ok:
            self.store.send(self.agent, delivery.sender, f"Hotovo #{mid}:\n{body}",
                            kind=MessageKind.RESULT, reply_to=mid)
            self.store.ack(mid, self.agent, DeliveryState.SUCCEEDED, lease_token=token)
        elif result.rate_limited:
            until = time.time() + result.retry_after
            self.store.ack(mid, self.agent, DeliveryState.FAILED, lease_token=token,
                           error=f"rate limit, znovu po {int(result.retry_after)} s")
            self.store.retry(mid, self.agent, not_before=until)
            self.store.heartbeat(self.agent, status="rate-limited (bridge)")
            # nahlásit limit → zbytek fronty se přesune na náhradu, ne zamrzne
            self.store.report_limit(self.agent, until, "limit poskytovatele (bridge)")
            self.store.send(self.agent, delivery.sender,
                            f"Task #{mid} odložen: limit poskytovatele, zkusím za "
                            f"{int(result.retry_after // 60)} min.",
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
    p.add_argument("--agent", required=True, help="komu patří fronta (codex, claude-code, …)")
    p.add_argument("--command", default=None,
                   help="šablona příkazu s {prompt}; výchozí podle agenta")
    p.add_argument("--provider", choices=["cli", "openrouter"], default="cli",
                   help="cli = codex/claude headless; openrouter = libovolný model přes API")
    p.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", ""),
                   help="např. anthropic/claude-sonnet-4.5 (jen pro --provider openrouter)")
    p.add_argument("--max-per-hour", type=int, default=12)
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--once", action="store_true", help="zpracuj jeden průchod a skonči")
    args = p.parse_args(argv)
    runner = None
    command = args.command or DEFAULT_COMMANDS.get(args.agent) or ""
    if args.provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key or not args.model:
            print("OpenRouter potřebuje OPENROUTER_API_KEY v prostředí a --model.", file=sys.stderr)
            return 2

        def runner(prompt: str) -> RunResult:
            return run_openrouter(prompt, model=args.model, api_key=key, timeout=args.timeout)
    elif not command:
        print("Chybí --command a pro tohoto agenta není výchozí.", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    bridge = Bridge(open_store(settings), args.agent, command, runner=runner,
                    timeout=args.timeout, max_per_hour=args.max_per_hour)
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
