#!/usr/bin/env python3
"""Fail-closed phase progression for Agent Lease. No agent execution or deployment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

PHASES = ("A", "B", "C", "D")
GATES = {
    "A": ("phase_a_review", "chaos_8h", "windows_wsl_restart",
          "process_supervision", "sqlite_fastmcp", "security"),
    "B": ("review_state_machine", "retry_budget", "diff_security_scan",
          "routing_and_locks"),
    "C": ("isolated_worktree", "repair_loop", "budget_enforcement",
          "write_mode_human_approval"),
    "D": ("shared_memory", "operation_modes", "research_agents",
          "verified_sources", "research_budget"),
}

def new_state() -> dict:
    return {"version": 1, "phase": "A", "evidence": {}, "history": []}

def load(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Missing state file: {path}. Run init first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != 1
            or data.get("phase") not in PHASES or not isinstance(data.get("evidence"), dict)
            or not isinstance(data.get("history"), list)):
        raise ValueError("Invalid phase state schema")
    return data

def remaining(state: dict) -> list[str]:
    evidence = state["evidence"].get(state["phase"], {})
    return [name for name in GATES[state["phase"]]
            if not isinstance(evidence.get(name), dict)
            or evidence[name].get("verified") is not True
            or not isinstance(evidence[name].get("reference"), str)
            or not evidence[name]["reference"].strip()]

def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".phase-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".agent-lease-phase.json"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    evidence = sub.add_parser("record")
    evidence.add_argument("gate")
    evidence.add_argument("reference", help="Link to test results, review, or commit")
    evidence.add_argument("--verified", action="store_true",
                          help="Only after a human has checked the evidence")
    advance = sub.add_parser("advance")
    advance.add_argument("--confirm", action="store_true",
                         help="Explicit human confirmation; never automatic")
    args = parser.parse_args(argv)
    if args.cmd == "init":
        if args.state.exists():
            parser.error("State already exists; refusing to overwrite")
        save(args.state, new_state())
        print("Initialized phase A (inactive; no agents started)")
        return 0
    try:
        state = load(args.state)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if args.cmd == "status":
        print(json.dumps({"phase": state["phase"], "missing": remaining(state),
                          "ready": not remaining(state)}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "record":
        if args.gate not in GATES[state["phase"]]:
            parser.error("Unknown gate for current phase")
        if not args.reference.strip():
            parser.error("Evidence reference must not be empty")
        state["evidence"].setdefault(state["phase"], {})[args.gate] = {
            "reference": args.reference, "verified": args.verified}
        save(args.state, state)
        print(f"Recorded {args.gate}; verified={args.verified}")
        return 0
    missing = remaining(state)
    if missing:
        print("BLOCKED; missing verified evidence:", ", ".join(missing))
        return 2
    if state["phase"] == "D":
        print("All phases recorded complete; no next phase")
        return 0
    if not args.confirm:
        print(f"READY to move from {state['phase']} to {PHASES[PHASES.index(state['phase']) + 1]}; "
              "requires --confirm by human")
        return 0
    target = PHASES[PHASES.index(state["phase"]) + 1]
    state["history"].append({"from": state["phase"], "to": target,
                             "evidence": state["evidence"][state["phase"]]})
    state["phase"] = target
    save(args.state, state)
    print(f"Advanced to phase {target}; no agents started")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
