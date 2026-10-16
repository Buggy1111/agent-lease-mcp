"""
Samostatný PreToolUse hook: hlídá obsah Bash příkazů, ne soubory.

⚠️ Schválně oddělené od `guard.py`/`policy.py` (nájmy na cesty). Ty řeší
„kdo drží soubor" a jsou proto deterministické a bezpečně fail-open i pro
Bash — cena falešného bloku tam byla vyšší než přínos (viz komentář u
`WRITE_TOOLS` v `policy.py`). Tady je úloha jiná — „je tenhle příkaz
nebezpečný" — a má proto jinou strukturu: natvrdo napsaná pravidla pro
jistá jednosměrná rizika, volitelně Jev (TypeSafe AI, popsaný v
`~/dev/jev-integrace-dokumentace.md`) na šedou zónu. Důvody podrobně
v `docs/adr/0003-jev-samostatny-guard.md`.

Chování, jakmile je tenhle hook zapojený v settings.json:
- natvrdo blokované vzory (`jev_policy.hard_block`) platí VŽDY, bez Jevu
  a bez ohledu na proměnné níž — nepotřebují klasifikátor.
- Jev se nevolá vůbec, dokud `JEV_GUARD_ENABLED=true` (potřebuje API klíč
  a každé volání něco stojí — default je vypnuto).
- i zapnutý Jev jen LOGUJE (`JEV_GUARD_MODE=shadow`, výchozí), dokud
  `JEV_GUARD_MODE=enforce` neřekne jinak — shadow mode napřed, jak
  doporučuje zdrojová dokumentace (bod 6, krok 2).
- na chybu Jevu (síť, timeout, špatná odpověď) hook fail-open pustí dál,
  stejně jako lease `guard.py` — rozbitá klasifikace nesmí zastavit práci.
- Jev je pravděpodobnostní a příkaz je text od agenta: dá se ho přesvědčit
  (prompt injection v komentáři příkazu) a v shadow režimu nic neblokuje.
  Je to doplňková vrstva na šedou zónu, NIKDY jediná ochrana — jistá rizika
  řeší deterministická pravidla výš a sandbox klienta.
- do logu i k Jevu jde příkaz se zamaskovanými tajnými hodnotami
  (`jev_policy.redact`), log je `0600`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .jev_client import JevAnswer, JevError, classify_command
from .jev_policy import decide_from_jev, hard_block, redact

EXIT_BLOCK = 2


def _log_path() -> Path:
    raw_db = os.environ.get("AGENT_LEASE_DB")
    db_dir = Path(raw_db).expanduser().parent if raw_db else Path.home() / ".agent-lease"
    return db_dir / "jev-guard.log"


def _log(entry: dict) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Log obsahuje příkazy (i zamaskované prozradí, na čem se pracuje) — jen
        # pro vlastníka, stejně jako room.db a webui.token.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logování nesmí shodit hook


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _bash_command(payload: dict) -> str | None:
    tool = payload.get("tool_name") or payload.get("toolName") or payload.get("tool") or ""
    if tool != "Bash":
        # Codex má pro shell vlastní jméno nástroje, zatím tady neověřené
        # (viz README, sekce Jev guard) — schválně nehádám tvar naslepo.
        return None
    raw_input = payload.get("tool_input") or payload.get("toolInput") or payload.get("input") or {}
    if not isinstance(raw_input, dict):
        return None
    command = raw_input.get("command")
    return command if isinstance(command, str) and command.strip() else None


def evaluate(payload: dict, classify=classify_command) -> tuple[int, str | None]:
    """
    Rozhodnutí bez stdin/exit — testovatelné přímo, `classify` jde nahradit
    fake funkcí místo skutečné sítě (viz `tests/test_jev_guard.py`).
    """
    command = _bash_command(payload)
    if command is None:
        return 0, None

    reason = hard_block(command)
    if reason:
        _log(
            {
                "ts": time.time(),
                "command": redact(command),
                "source": "hard-rule",
                "verdict": "block",
                "reason": reason,
            }
        )
        return EXIT_BLOCK, (
            f"[jev-guard] blocked by a hard rule: {reason}. "
            f"If this is really needed, ask the human directly."
        )

    if os.environ.get("JEV_GUARD_ENABLED", "false").lower() != "true":
        return 0, None

    enforce = os.environ.get("JEV_GUARD_MODE", "shadow").lower() == "enforce"
    threshold = _float_env("JEV_GUARD_THRESHOLD", 0.5)

    try:
        # Ven jde jen zamaskovaný příkaz: klíče z příkazové řádky nemají co
        # dělat u třetí strany.
        answer: JevAnswer = classify(redact(command))
    except JevError as exc:
        _log({"ts": time.time(), "command": redact(command), "source": "jev-error", "error": str(exc)})
        return 0, f"[jev-guard] classifier unavailable, letting the call through: {exc}"

    decision = decide_from_jev(answer.p_destructive, threshold=threshold, enforce=enforce)
    _log(
        {
            "ts": time.time(),
            "command": redact(command),
            "p_destructive": answer.p_destructive,
            "threshold": threshold,
            "source": decision.source,
            "verdict": decision.verdict.value,
        }
    )

    if decision.blocks:
        return EXIT_BLOCK, decision.reason
    return 0, None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # nerozumím vstupu → pustit dál, ne blokovat naslepo

    exit_code, message = evaluate(payload)
    if message:
        print(message, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
