"""
Pravidla. Čisté funkce, žádné IO — tohle je jediná část, která se bude
pravidelně měnit (přibude nástroj, změní se pokrytí cest), a proto je oddělená
od SQLite mechaniky pod ní i od MCP slupky nad ní.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .models import Claim, Decision, Verdict

# Nástroje, které mění soubory. Claude Code i Codex, každý si je jmenuje jinak.
#
# Bash tu schválně NENÍ: zjistit z příkazové řádky, co všechno skript zapíše, je
# nespolehlivé. Falešné blokování by bylo horší než žádné — agent by se naučil
# hook obcházet. Na skripty sahající na sdílené věci je explicitní `claim`.
WRITE_TOOLS = frozenset(
    {
        "Edit", "Write", "NotebookEdit", "MultiEdit",                    # Claude Code
        "edit", "write", "apply_patch", "str_replace", "create_file",    # Codex
    }
)

# Klíče, pod kterými klienti nesou cestu k souboru.
_PATH_KEYS = ("file_path", "path", "filePath", "target_file", "notebook_path")


def normalize_path(path: str) -> str:
    """
    Absolutní cesta bez symlinků.

    ⚠️ Bez tohohle je nájem k ničemu: jeden agent si vezme `deploy/x.sh`, druhý
    `/home/…/deploy/x.sh` a oba si myslí, že mají volno.
    """
    return str(Path(path).expanduser().resolve())


def covers(claim_path: str, target: str) -> bool:
    """
    Nájem na adresář pokrývá i soubory pod ním.

    Bez tohohle je celá věc k ničemu pro reálnou práci: nikdo nevyjmenuje dopředu
    všechny soubory, kterých se dotkne. Právě takhle vznikla kolize, kvůli které
    tenhle projekt existuje — jeden agent „dělal na deploy/", druhý si vzal
    jeden konkrétní soubor uvnitř.
    """
    if claim_path == target:
        return True
    return target.startswith(claim_path.rstrip("/") + "/")


def extract_target(payload: dict) -> tuple[str, str | None]:
    """Jméno nástroje a cesta z payloadu hooku; tvary se mezi klienty liší."""
    tool = payload.get("tool_name") or payload.get("toolName") or payload.get("tool") or ""
    raw_input = payload.get("tool_input") or payload.get("toolInput") or payload.get("input") or {}
    if not isinstance(raw_input, dict):
        return tool, None
    for key in _PATH_KEYS:
        value = raw_input.get(key)
        if isinstance(value, str) and value:
            return tool, value
    return tool, None


def decide(tool: str, path: str | None, holder: Claim | None, me: str) -> Decision:
    """
    Smí agent `me` sáhnout na `path`?

    Čistá funkce: dostane stav, vrátí rozhodnutí. Nic nezapisuje — zápis
    (auto-nájem, audit) dělá volající, aby se tohle dalo testovat bez databáze.
    """
    if tool not in WRITE_TOOLS or not path:
        return Decision(verdict=Verdict.ALLOW, reason="nehlídaný nástroj")

    target = normalize_path(path)

    if holder is None:
        return Decision(verdict=Verdict.AUTO_CLAIMED, path=target)

    if holder.agent == me:
        return Decision(verdict=Verdict.ALLOW, path=target, reason="už je můj")

    return Decision(
        verdict=Verdict.DENY,
        path=target,
        blocked_by=holder,
        reason=(
            f"[agent-lease] {Path(target).name} má rozpracovaný {holder.agent}"
            f" ({holder.purpose or 'bez popisu'}), nájem vyprší za {holder.expires_in} s.\n"
            f"Nesahej na něj. Vezmi si jinou práci, nebo se ozvi přes `say`."
        ),
    )


# ── spouštěné skripty ────────────────────────────────────────────────────────
# Bash obecně hlídat nejde, ALE jeden případ ano a je to přesně ten, který
# 9. 9. 2026 způsobil škodu: jeden agent skript SPOUŠTĚL, druhý ho zároveň
# EDITOVAL. Bash čte skript průběžně, takže se mu text posunul pod rukama.
# Spouštěný soubor si proto na dobu běhu bereme do nájmu.
_RUNNERS = frozenset({"bash", "sh", "zsh", "python", "python3", "node", "npx", "uv", "poetry"})
_SCRIPT_SUFFIXES = (".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts", ".ps1")


def script_target(command: str) -> str | None:
    """
    Cesta ke skriptu, který ten příkaz spouští, nebo None.

    Schválně úzké: falešné zabrání cesty je horší než žádné, protože by agenta
    naučilo hook obcházet. Proto musí soubor existovat a být buď za známým
    interpretem, nebo spuštěný přes `./`.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None

    candidate: str | None = None
    if tokens[0].startswith("./"):
        candidate = tokens[0]
    elif Path(tokens[0]).name in _RUNNERS:
        candidate = next((t for t in tokens[1:] if not t.startswith("-")), None)

    if not candidate or not candidate.endswith(_SCRIPT_SUFFIXES):
        return None
    return candidate if Path(candidate).expanduser().is_file() else None
