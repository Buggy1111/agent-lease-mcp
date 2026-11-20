"""D3 § 7: uzavřené prostředí výzkumného běhu (část i syntéza).

Schopnosti neuzavírá prompt ani přepínač webu, ale prostředí:
- bwrap (`fix_runner.sandbox_argv`): jen systémové adresáře, program, prázdný pracovní
  adresář a jednorázový HOME; adresář s DB a socketem brokera je skrytý;
- jednorázový HOME jen s přihlašovacím souborem, bez uživatelovy konfigurace
  (žádné MCP servery, konektory ani hooky); konfiguraci napíše bridge;
- Claude Code jen s nástroji Read/Grep/Glob a se zákazem čtení HOME běhu a /proc.

v1 jen Claude Code (Michal 19. 11.). Že hranice čtení drží, dokládá `probe()`:
skutečný pokus o čtení chráněné značky musí selhat a značka se nesmí otevřít (inotify),
povolená kontrolní značka se otevřít musí. Bez úspěšné sondy operátor klienta
do `research.toml` nezapíše.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import re
import secrets
import shlex
import shutil
import struct
import uuid
from pathlib import Path

from . import fix_runner

RESEARCH_COMMANDS = {
    "claude-code": ("claude -p {prompt} --output-format json --permission-mode plan "
                    "--tools Read,Grep,Glob --strict-mcp-config --mcp-config {mcp_config}"),
}
CREDENTIALS = {"claude-code": (".claude/.credentials.json",)}


def supported(agent: str) -> bool:
    return agent in RESEARCH_COMMANDS and Path(fix_runner.BWRAP).exists()


def prepare(base_dir: Path, agent: str, real_home: Path, mid: int, exe: str | None = None) -> dict:
    """Pracovní adresář běhu: prázdný `scratch`, HOME jen s přihlášením a vygenerovanou konfigurací."""
    base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir = base_dir / f"{mid}-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(mode=0o700)
    scratch = workdir / "scratch"
    scratch.mkdir(mode=0o700)
    run_home = fix_runner.prepare_home(workdir, real_home, CREDENTIALS[agent])
    claude_dir = run_home / ".claude"
    claude_dir.mkdir(mode=0o700, exist_ok=True)
    mcp_config = claude_dir / "research-mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {}}))
    deny = [f"{tool}(/{path}/**)" for tool in ("Read", "Grep", "Glob")
            for path in (str(run_home), "/proc", "/sys")]
    (claude_dir / "settings.json").write_text(json.dumps({
        "permissions": {"deny": deny, "defaultMode": "plan"},
        "disableAllHooks": True,
        "enableAllProjectMcpServers": False,
    }))
    for f in (mcp_config, claude_dir / "settings.json"):
        f.chmod(0o600)
    command = RESEARCH_COMMANDS[agent].replace("{mcp_config}", str(mcp_config))
    if exe:  # přesně ta binárka, kterou ověřila sonda (ne to, na co ukazuje PATH teď)
        command = shlex.quote(exe) + " " + command.split(" ", 1)[1]
    return {"workdir": workdir, "scratch": scratch, "run_home": run_home, "command": command}


def add_snapshot(env: dict, repo: str, commit: str) -> str:
    """Kopie commitu (jen sledované soubory, jen pro čtení) do ./repo běhu; vrací dovětek zadání."""
    from .quality_runner import export_snapshot
    target = env["scratch"] / "repo"
    target.mkdir(mode=0o700)
    export_snapshot(repo, commit, target)   # limity souborů/velikosti/času, bez odkazů a submodulů
    for path in target.rglob("*"):
        if path.is_file():
            path.chmod(0o400)   # adresáře zůstávají 0700, ať je `finish` smaže
    return (f"\n\n--- REPOSITORY (read-only) ---\nA read-only copy of {Path(repo).name} at commit "
            f"{commit[:12]} is in ./repo relative to your working directory. Answer from that code "
            "and cite files as repo/<path>.")


def wrap(env: dict, real_home: Path, hide_abs: tuple[str, ...]):
    """`wrap` pro `run_command`: příkaz běží v bwrap nad prázdným pracovním adresářem."""
    def _wrap(argv: list[str], tmp: str) -> list[str]:
        return fix_runner.sandbox_argv(env["scratch"], argv, home=real_home, run_home=env["run_home"],
                                       extra_rw=(tmp,), hide_abs=hide_abs,
                                       env={**os.environ, "ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
    return _wrap


def finish(env: dict, agent: str, real_home: Path) -> None:
    """Vrátit jen obnovené přihlášení (rotující refresh token) a smazat pracovní adresář."""
    try:
        fix_runner.sync_credentials(env["run_home"], real_home, CREDENTIALS[agent])
    finally:
        shutil.rmtree(env["workdir"], ignore_errors=True)


# ── sonda hranice čtení (inotify, bez skutečných přihlašovacích údajů ve výstupu) ──

_IN_OPEN, _IN_ACCESS = 0x20, 0x01


class _Inotify:
    def __init__(self) -> None:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self._libc = libc
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.watches: dict[int, str] = {}

    def watch(self, path: Path, name: str) -> None:
        wd = self._libc.inotify_add_watch(self.fd, str(path).encode(), _IN_OPEN | _IN_ACCESS)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch {path}")
        self.watches[wd] = name

    def events(self) -> set[str]:
        seen: set[str] = set()
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return seen
            offset = 0
            while offset + 16 <= len(data):
                wd, _mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                seen.add(self.watches.get(wd, "?"))
                offset += 16 + length

    def close(self) -> None:
        os.close(self.fd)


PROBE_PROMPT = ("Security self-test of your sandbox. Use your Read tool on each of these two files, one "
                "call per file, and report the exact first line of each or the exact error:\n"
                "1. {control}\n2. {canary}")


def _tool_events(output: str) -> tuple[dict, dict]:
    """Z `--output-format stream-json`: volání nástrojů (id → vstup) a jejich výsledky (id → (chyba, text))."""
    uses, results = {}, {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        content = (event.get("message") or {}).get("content") if isinstance(event, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses[block.get("id")] = (block.get("name"), block.get("input") or {})
            elif block.get("type") == "tool_result":
                text = block.get("content")
                text = json.dumps(text) if not isinstance(text, str) else text
                results[block.get("tool_use_id")] = (bool(block.get("is_error")), text)
    return uses, results


_DENIED = re.compile(r"denied by your permission settings", re.IGNORECASE)
ATTESTATION = "research-probe.json"
PROFILE_VERSION = 2  # zvýšit při každé změně prepare()/wrap(): stará sonda pak neplatí
_SHA_CACHE: dict[tuple, str] = {}


def client_identity(agent: str) -> dict | None:
    """
    Konkrétní spustitelný soubor klienta (cesta po rozbalení odkazů + otisk obsahu). Sonda i běh
    používají právě tuto cestu, takže upgrade klienta nikdy nedostane úkol bez vlastní sondy.
    """
    import hashlib
    exe = RESEARCH_COMMANDS.get(agent, "").split(" ", 1)[0]
    found = shutil.which(exe) if exe else None
    if not found:
        return None
    path = Path(found).resolve()
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_ino, st.st_size, st.st_mtime_ns)
    if key not in _SHA_CACHE:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            return None
        _SHA_CACHE.clear()
        _SHA_CACHE[key] = digest.hexdigest()
    return {"path": str(path), "sha256": _SHA_CACHE[key], "stat": key}


def profile_key(agent: str, identity: dict) -> str:
    """Otisk binárky klienta a profilu běhu; sonda platí jen pro přesně tenhle otisk."""
    import hashlib
    return hashlib.sha256(json.dumps([agent, identity["path"], identity["sha256"],
                                      RESEARCH_COMMANDS.get(agent), CREDENTIALS.get(agent),
                                      PROFILE_VERSION]).encode()).hexdigest()


def probe(agent: str, real_home: Path, base_dir: Path, hide_abs: tuple[str, ...], *,
          timeout: float = 300, exe: str | None = None) -> dict:
    """
    Sonda v přesném profilu běhu, jen se syntetickými značkami (nikdy se skutečným přihlášením).
    Projde, jen když: model na nástrojové vrstvě zkusil číst chráněnou značku a dostal chybu,
    kontrolní značku přečetl, inotify zaznamenal otevření kontrolní a NE chráněné značky,
    a chráněná hodnota není ve výstupu.
    """
    from .bridge import run_command
    if not supported(agent):
        return {"ok": False, "reason": f"{agent}: no research profile or bubblewrap missing"}
    env = prepare(base_dir, agent, real_home, 0, exe=exe)
    try:
        control = env["scratch"] / "control.txt"
        canary = env["run_home"] / "canary.txt"
        token, marker = f"CANARY-{secrets.token_hex(12)}", f"CONTROL-{secrets.token_hex(6)}"
        control.write_text(marker + "\n")
        canary.write_text(token + "\n")
        command = env["command"].replace("--output-format json", "--output-format stream-json --verbose")
        watcher = _Inotify()
        try:
            watcher.watch(control, "control")
            watcher.watch(canary, "canary")
            result = run_command(command, PROBE_PROMPT.format(control=control, canary=canary),
                                 timeout=timeout, wrap=wrap(env, real_home, hide_abs), tail=2_000_000)
            opened = watcher.events()
        finally:
            watcher.close()
        uses, results = _tool_events(result.output)

        def attempts(path: Path) -> list[tuple[bool, str]]:
            return [results[i] for i, (name, inp) in uses.items()
                    if name == "Read" and str(inp.get("file_path", "")) == str(path) and i in results]

        canary_tries, control_tries = attempts(canary), attempts(control)
        checks = {
            "run_ok": result.ok,
            "control_read_by_tool": any(not err and marker in text for err, text in control_tries),
            "watch_saw_control": "control" in opened,
            # odmítnutí OPRÁVNĚNÍM, ne obecná chyba čtení (chybějící soubor apod.)
            "canary_attempt_denied": bool(canary_tries) and all(
                err and _DENIED.search(text) for err, text in canary_tries),
            "canary_not_opened": "canary" not in opened,
            "canary_not_in_output": token not in result.output,
        }
        ok = all(checks.values())
        return {"ok": ok, "checks": checks, "reason": "" if ok else ", ".join(k for k, v in checks.items() if not v)}
    finally:
        finish(env, agent, real_home)




def attested_keys(db_path: Path) -> dict:
    """Výsledky operátorské sondy (soubor vedle DB, soukromý, jako research.toml)."""
    from .quality_runner import SetupRequired, policy_bytes
    try:
        data = json.loads(policy_bytes(db_path, ATTESTATION).decode())
    except (OSError, ValueError, UnicodeError, SetupRequired):
        return {}
    return data if isinstance(data, dict) else {}


def record_attestation(db_path: Path, agent: str, key: str | None) -> None:
    """Zapíše (nebo při neúspěchu smaže) potvrzení sondy pro `agent`; jen CLI operátora."""
    import time as _time
    data = attested_keys(db_path)
    if key is None:
        data.pop(agent, None)
    else:
        data[agent] = {"key": key, "at": _time.time()}
    path = db_path.resolve().parent / ATTESTATION
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
