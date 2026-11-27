"""Run sandbox E2E inside an explicitly bounded, disposable delegated scope."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from agent_lease_mcp.quality_runner import verify_limits


def main() -> None:
    verify_limits()
    group = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                 if line.startswith("0::"))
    root = Path("/sys/fs/cgroup") / group.lstrip("/")
    if not root.name.startswith("agent-lease-quality-e2e-") or not root.name.endswith(".scope"):
        raise RuntimeError("E2E requires its own disposable delegated scope")
    broker = root / "broker"
    broker.mkdir()
    (broker / "cgroup.procs").write_text(str(os.getpid()))
    (root / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    os.environ["AGENT_LEASE_QUALITY_E2E"] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "pytest", "-q",
                            "tests/test_quality_runner_e2e.py", *sys.argv[1:]])


if __name__ == "__main__":
    main()
