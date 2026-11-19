"""Verify an installed runtime against synthetic broker/MCP/bridge data only.

Run with the candidate interpreter, outside the repository (no PYTHONPATH).
The output directory must be a new directory; no existing database is accepted.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_lease_mcp.broker import RemoteStore
from agent_lease_mcp.config import Settings


def runtime() -> dict:
    import agent_lease_mcp

    with sqlite3.connect(":memory:") as connection:
        sql_version = connection.execute("SELECT sqlite_version()").fetchone()[0]
    assert sqlite3.sqlite_version_info >= (3, 51, 3), sqlite3.sqlite_version
    assert sql_version == sqlite3.sqlite_version
    return {"python": sys.version.split()[0], "executable": sys.executable,
            "sqlite": sql_version, "package": str(Path(agent_lease_mcp.__file__).resolve()),
            "fastmcp": importlib.metadata.version("fastmcp")}


async def check_mcp(env: dict, root: Path, wrapper: Path) -> int:
    parameters = StdioServerParameters(command=sys.executable,
                                      args=[str(wrapper), "agent_lease_mcp.server"],
                                      env={**env, "AGENT_NAME": "a8-mcp"})
    with (root / "mcp.stderr").open("w") as errors:
        async with stdio_client(parameters, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {"claim", "owner", "release", "room"} <= {t.name for t in tools.tools}
                target = str(root / "synthetic-target.txt")
                for name, arguments in [("claim", {"paths": [target]}),
                                        ("owner", {"path": target}),
                                        ("release", {"paths": [target]})]:
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, response
                    data = response.structuredContent
                    if data is None:
                        data = json.loads(response.content[0].text)
                    if name == "claim":
                        assert data["ok"] is True
                    if name == "owner":
                        assert data["held_by"] == "a8-mcp"
                return len(tools.tools)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    result = {"runtime": runtime()}
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "AGENT_LEASE_DB": str(root / "room.db"), "AGENT_LEASE_SOCKET": str(root / "b.sock"),
           "AGENT_LEASE_REQUIRE_BROKER": "1", "AGENT_LEASE_HUMAN": "michal",
           "AGENT_NAME": "a8-check"}
    # Probe each real service interpreter before entering its unmodified main.
    wrapper = root / "launch.py"
    wrapper.write_text(
        "import json,runpy,sqlite3,sys\n"
        "assert sqlite3.sqlite_version == '" + sqlite3.sqlite_version + "'\n"
        "print('A8_RUNTIME '+json.dumps({'sqlite':sqlite3.sqlite_version,"
        "'executable':sys.executable}),file=sys.stderr,flush=True)\n"
        "module=sys.argv.pop(1)\nrunpy.run_module(module,run_name='__main__')\n")
    broker_log = (root / "broker.stderr").open("w")
    broker = subprocess.Popen([sys.executable, str(wrapper), "agent_lease_mcp.broker"],
                              env=env, cwd=root, stdout=broker_log, stderr=broker_log)
    try:
        end = time.monotonic() + 15
        while not Path(env["AGENT_LEASE_SOCKET"]).exists():
            assert broker.poll() is None, "broker exited before readiness"
            assert time.monotonic() < end, "broker readiness timeout"
            time.sleep(0.05)
        store = RemoteStore(Settings(Path(env["AGENT_LEASE_DB"]), "a8-check", 1800),
                            Path(env["AGENT_LEASE_SOCKET"]))
        result["mcp_tool_count"] = asyncio.run(check_mcp(env, root, wrapper))
        mid = store.send("michal", "a8-worker", "Synthetic runtime verification", kind="task")
        worker = root / "worker.py"
        worker.write_text(
            "import json,sqlite3\nfrom pathlib import Path\n"
            "assert sqlite3.sqlite_version == '" + sqlite3.sqlite_version + "'\n"
            "Path('worker.json').write_text(json.dumps({'sqlite':sqlite3.sqlite_version}))\n"
            "print('synthetic runtime done')\n")
        command = shlex.join([sys.executable, str(worker), "{prompt}"])
        completed = subprocess.run(
            [sys.executable, str(wrapper), "agent_lease_mcp.bridge", "--agent", "a8-worker",
             "--command", command, "--once", "--include-existing", "--interactive-grace", "0"],
            env=env, cwd=root, capture_output=True, text=True, timeout=30, check=False)
        (root / "bridge.stderr").write_text(completed.stderr)
        assert completed.returncode == 0, completed.stderr
        jobs = store.jobs("a8-worker", viewer="michal")
        assert next(job for job in jobs if job["id"] == mid)["state"] == "succeeded", jobs
        result["worker"] = json.loads((root / "worker.json").read_text())
        result["backup"] = store.backup_now()
        assert result["backup"]["backup_integrity"] == "ok"
        result["bridge_completed"] = True
    finally:
        broker.terminate()
        try:
            broker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.wait(timeout=5)
        broker_log.close()
    for name in ["broker", "mcp", "bridge"]:
        lines = (root / f"{name}.stderr").read_text().splitlines()
        probes = [json.loads(line.removeprefix("A8_RUNTIME ")) for line in lines
                  if line.startswith("A8_RUNTIME ")]
        assert len(probes) == 1 and probes[0]["sqlite"] == sqlite3.sqlite_version, name
        result[name + "_runtime"] = probes[0]
    with sqlite3.connect(root / "room.db") as connection:
        result["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert result["integrity"] == "ok"
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
