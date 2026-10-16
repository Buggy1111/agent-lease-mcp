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
        return Decision(verdict=Verdict.ALLOW, reason="unguarded tool")

    target = normalize_path(path)

    if holder is None:
        return Decision(verdict=Verdict.AUTO_CLAIMED, path=target)

    if holder.agent == me:
        return Decision(verdict=Verdict.ALLOW, path=target, reason="already mine")

    return Decision(
        verdict=Verdict.DENY,
        path=target,
        blocked_by=holder,
        reason=(
            f"[agent-lease] {Path(target).name} is being worked on by {holder.agent}"
            f" ({holder.purpose or 'no description'}); the lease expires in {holder.expires_in} s.\n"
            f"Do not touch it. Take other work, or ask via `say`/`send`."
        ),
    )


# ── spouštěné skripty ────────────────────────────────────────────────────────
# Bash obecně hlídat nejde, ALE jeden případ ano a je to přesně ten, který
# 9. 9. 2026 způsobil škodu: jeden agent skript SPOUŠTĚL, druhý ho zároveň
# EDITOVAL. Bash čte skript průběžně, takže se mu text posunul pod rukama.
# Spouštěný soubor si proto na dobu běhu bereme do nájmu.
# Příkazy, které jen spouští další příkaz — skutečný příkaz je až za nimi.
_WRAPPERS = frozenset({"sudo", "env", "command", "nice", "nohup", "time", "exec", "doas", "stdbuf"})
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
    while tokens and (Path(tokens[0]).name in _WRAPPERS or "=" in tokens[0]):
        tokens = tokens[1:]  # `sudo bash x.sh`, `env A=1 ./x.sh`
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


# ── zápisy z příkazové řádky (APP-006) ───────────────────────────────────────
# Obecný Bash pořád neparsujeme. Jen několik tvarů, kde je cíl zápisu přímo
# vidět: přesměrování `>`/`>>`, `tee`, `sed -i`, `truncate`, cíl `cp`/`mv`.
# Tyhle cesty se NEberou do nájmu (to by zamykalo každý log v /tmp) — jen se
# zablokuje zápis do souboru, který drží někdo jiný. Falešný blok tím nevznikne:
# blokuje se výhradně skutečná kolize.
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_MAX_NESTING = 3


def shell_write_targets(command: str, _depth: int = 0) -> list[str]:
    """
    Cesty, do kterých příkaz zjevně zapíše. Prázdný seznam, když nevím.

    ⚠️ Best effort proti omylu, ne hranice proti agentovi, který se snaží
    obejít hook — shell se úplně rozparsovat nedá (`eval`, proměnné, aliasy).
    """
    if _depth > _MAX_NESTING:
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []

    targets: list[str] = []
    segment: list[str] = []

    def flush() -> None:
        # Obaly (`sudo`, `env A=1`, `nohup`…) a přiřazení proměnných před
        # příkazem přeskočit; `bash -c "…"` projít rekurzivně.
        while segment and (
            Path(segment[0]).name in _WRAPPERS
            or ("=" in segment[0] and not segment[0].startswith(("-", "=")))
            or (segment[0].startswith("-") and len(segment) > 1)
        ):
            segment.pop(0)
        if not segment:
            return
        name = Path(segment[0]).name
        if name in _SHELLS and "-c" in segment[1:]:
            idx = segment.index("-c")
            if idx + 1 < len(segment):
                targets.extend(shell_write_targets(segment[idx + 1], _depth + 1))
            segment.clear()
            return
        args = [t for t in segment[1:] if not t.startswith("-")]
        flags = [t for t in segment[1:] if t.startswith("-")]
        if name == "tee":
            targets.extend(args)
        elif name == "sed" and any(f.startswith("-i") or f == "--in-place" for f in flags):
            targets.extend(args[1:])  # první argument je skript sedu
        elif name == "truncate":
            targets.extend(args[1:] if "-s" in flags else args)
        elif name in ("cp", "mv") and len(args) >= 2:
            targets.append(args[-1])
        segment.clear()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _REDIRECTS:
            if i + 1 < len(tokens):
                targets.append(tokens[i + 1])
            i += 2
            continue
        if tok in _SEPARATORS:
            flush()
        elif not (tok.isdigit() and i + 1 < len(tokens) and tokens[i + 1] in _REDIRECTS):
            segment.append(tok)
        i += 1
    flush()
    return [t for t in targets if t and not t.startswith(("/dev/", "&", "$"))]
