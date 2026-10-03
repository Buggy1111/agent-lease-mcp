"""
`agent-lease init` — nastaví hooky klienta za tebe (idempotentně, se zálohou).

Ruční vkládání JSONu je přesně místo, kde koordinace tiše zmizí. Příkaz proto
sám zapíše všech pět hooků (guard, context, release, + volitelné probuzení) a
další spuštění ty stávající agent-lease záznamy nahradí, ne zdvojí.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

CLIENTS = {
    "claude-code": {"file": Path(".claude") / "settings.json", "matcher": "Edit|Write|NotebookEdit|MultiEdit|Bash"},
    "codex": {"file": Path(".codex") / "hooks.json", "matcher": "*"},
}


def _bin(name: str) -> str:
    found = shutil.which(name)
    return found or str(Path(sys.executable).parent / name)


def build_hooks(agent: str, *, rewake: bool) -> dict:
    cfg = CLIENTS[agent]
    env = f"AGENT_NAME={agent}"
    ctx = {"type": "command", "command": f"{env} {_bin('agent-lease-context')}"}
    hooks: dict = {
        "PreToolUse": [{"matcher": cfg["matcher"], "hooks": [
            {"type": "command", "command": f"{env} {_bin('agent-lease-guard')}"}]}],
        "SessionStart": [{"hooks": [ctx]}],
        "UserPromptSubmit": [{"hooks": [ctx]}],
        "Stop": [{"hooks": [{"type": "command", "command": f"{env} {_bin('agent-lease-release')}"}]}],
    }
    if rewake:
        hooks["SessionStart"][0]["hooks"].append({
            "type": "command", "async": True, "asyncRewake": True,
            "command": f"{env} {_bin('agent-lease')} wait --for {agent} --rewake --timeout 86400",
        })
    return hooks


def _is_ours(entry: dict) -> bool:
    return any("agent-lease" in h.get("command", "") for h in entry.get("hooks", []))


def merge(existing: dict, ours: dict) -> dict:
    out = json.loads(json.dumps(existing))
    section = out.setdefault("hooks", {})
    for event, entries in ours.items():
        kept = [e for e in section.get(event, []) if not _is_ours(e)]
        section[event] = kept + entries
    return out


def run_init(agent: str, *, write: bool, rewake: bool, home: Path | None = None) -> int:
    if agent not in CLIENTS:
        print(f"Neznámý klient {agent!r}; podporováno: {', '.join(CLIENTS)}", file=sys.stderr)
        return 2
    path = (home or Path.home()) / CLIENTS[agent]["file"]
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except ValueError:
            print(f"{path} není platný JSON — neupravuji. Oprav ho ručně.", file=sys.stderr)
            return 1
    merged = merge(existing, build_hooks(agent, rewake=rewake))
    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    if not write:
        print(f"# {path}  (nic nezapsáno; přidej --write)\n{text}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text)
    print(f"Zapsáno {path}" + (" (záloha .bak)" if path.with_suffix(path.suffix + ".bak").exists() else ""))
    print("Ověř: agent-lease doctor")
    return 0
