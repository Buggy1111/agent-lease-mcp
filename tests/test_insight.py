"""D8–D11: čtecí zprávy (self-audit, recall benchmark, efektivita, zdraví kódu)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from agent_lease_mcp import health, insight
from agent_lease_mcp.cli import main as cli_main
from agent_lease_mcp.store import Store

SECRET = "sk-ant-api03-" + "Z" * 40


def _digest(root: Path) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


# ── D8 ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def broken_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "good").mkdir(parents=True)
    (home / ".claude" / "skills" / "copy").mkdir()
    (home / ".claude" / "skills" / "bare").mkdir()
    (home / ".codex").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": f"API_KEY={SECRET} /nonexistent/hook-bin --token {SECRET}"}]}]}}))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"ghost": {"command": "no-such-mcp-xyz",
                                                                            "env": {"KEY": SECRET}}}}))
    # rozbitý JSON, jehož text obsahuje tajemství — chyba parseru ho nesmí citovat
    (home / ".codex" / "hooks.json").write_text('{"hooks": "' + SECRET + '",,}')
    for name in ("good", "copy"):
        (home / ".claude" / "skills" / name / "SKILL.md").write_text("---\nname: dup\ndescription: x\n---\n")
    (home / ".claude" / "skills" / "bare" / "SKILL.md").write_text("no frontmatter")
    return home


def _room(tmp_path: Path) -> Path:
    db = tmp_path / "room.db"
    Store(db)
    now = time.time()
    con = sqlite3.connect(db)
    for mid, state, attempts, err in ((1, "needs_review", 1, f"research: client changed {SECRET}"),
                                      (2, "failed", 4, f"Traceback {SECRET}"),
                                      (3, "expired", 0, "")):
        con.execute("INSERT INTO messages(id, agent, text, sent_at, recipient, kind) VALUES (?,?,?,?,?,?)",
                    (mid, "michal", "t", now, "codex", "task"))
        con.execute("INSERT INTO deliveries(message_id, recipient, state, attempts, last_error, updated_at) "
                    "VALUES (?,?,?,?,?,?)", (mid, "codex", state, attempts, err, now))
    con.commit()
    con.close()
    return db


def test_self_audit_finds_broken_harness_without_leaking_or_writing(broken_home, tmp_path, monkeypatch):
    db = _room(tmp_path)
    monkeypatch.setenv("AGENT_LEASE_SOCKET", str(tmp_path / "no-broker.sock"))  # broker nedostupný
    before = _digest(broken_home) | _digest(tmp_path)
    report = insight.self_audit(broken_home, db, days=7)
    text = insight.self_audit_markdown(report) + json.dumps(report)
    assert _digest(broken_home) | _digest(tmp_path) == before
    areas = {(f["area"], f["problem"].split(" ")[0]) for f in report["findings"]}
    problems = " | ".join(f["problem"] + " " + f["cause"] for f in report["findings"])
    assert "codex hooks nejde načíst" in problems and "JSONDecodeError" in problems
    assert "nenalezeno: hook-bin" in problems
    assert "MCP server ghost" in problems and "nenalezeno: no-such-mcp-xyz" in problems
    assert "skill bez name/description" in problems and "skill dup je víckrát" in problems
    assert "kategorie důvodu: research" in problems and "kategorie důvodu: jiný" in problems
    assert ("waste", "1") in areas  # úkol s ≥ 3 pokusy
    assert "vypršelo" in problems
    assert SECRET not in text and "Traceback" not in text and "/nonexistent" not in text


def test_self_audit_cli_offline_writes_private_file(broken_home, tmp_path, monkeypatch):
    db = _room(tmp_path)
    monkeypatch.setenv("HOME", str(broken_home))
    monkeypatch.setenv("AGENT_LEASE_DB", str(db))
    monkeypatch.setenv("AGENT_LEASE_SOCKET", str(tmp_path / "no-broker.sock"))
    monkeypatch.delenv("AGENT_NAME", raising=False)
    db_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    out = tmp_path / "audit.md"
    assert cli_main(["self-audit", "--out", str(out)]) == 0
    assert out.stat().st_mode & 0o777 == 0o600
    assert SECRET not in out.read_text()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_hash
    assert not (tmp_path / "no-broker.sock").exists()


# ── D9 ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def memory(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "mem.db"
    s = Store(db)
    ids = {
        "hang_v": s.knowledge_add("claude-code", "pytest hangs on import", cause="WSL OOM", solution="-x")["id"],
        "hang_u": s.knowledge_add("claude-code", "pytest hangs in CI", cause="network")["id"],
        "hang_old": s.knowledge_add("codex", "pytest hangs old python", cause="3.10 bug")["id"],
        "bridge": s.knowledge_add("codex", "bridge does not start codex", cause="PATH")["id"],
    }
    s.knowledge_verdict("codex", ids["hang_v"], "verified", "commit abc")
    s.knowledge_verdict("claude-code", ids["hang_old"], "stale", "python 3.13")
    s.knowledge_verdict("claude-code", ids["bridge"], "verified", "test x")
    return db, ids


def test_recall_bench_metrics_follow_agent_search(memory):
    db, ids = memory
    cases = [
        {"query": "pytest hangs", "project": "", "expect": [ids["hang_v"]], "forbid": [ids["hang_u"]], "k": 5},
        {"query": "bridge codex", "project": "", "expect": [ids["bridge"]], "forbid": [], "k": 5},
        {"query": "pytest hangs", "project": "", "expect": [ids["hang_old"]], "forbid": [], "k": 5},
    ]
    with insight.snapshot(db) as con:
        rep = insight.recall_bench(con, cases)
    a, b, c = rep["cases"]
    assert a["returned"][0] == ids["hang_v"] and ids["hang_old"] not in a["returned"]  # ověřené první, vyřazené ne
    assert a["precision"] == 0.5 and a["recall"] == 1.0 and a["rr"] == 1.0
    assert a["wrong_recall"] == [ids["hang_u"]] and a["unverified_returned"] == [ids["hang_u"]]
    assert a["retired_diag"] == [ids["hang_old"]]  # jen diagnostika
    assert b["precision"] == b["recall"] == b["rr"] == 1.0
    assert c["outdated_case"] == [ids["hang_old"]]  # očekávaný je zastaralý → případ se nehodnotí
    s = rep["summary"]
    assert s["scored"] == 2 and s["outdated_cases"] == 1
    assert s["precision"] == 0.75 and s["recall"] == 1.0 and s["wrong_recall"] == 1
    md = insight.recall_markdown(rep, rep)
    assert "| precision | 0.750 | 0.750 | +0.000 |" in md
    other = dict(rep, fingerprint="different")
    with pytest.raises(ValueError, match="not comparable"):
        insight.recall_markdown(rep, other)


def test_recall_cases_file_validation(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[[case]]\nquery = "x"\nexpect = ["a"]\n')
    with pytest.raises(ValueError, match="integer"):
        insight.load_cases(f)
    f.write_text('[[case]]\nquery = "x"\nexpect = [1]\n')
    assert insight.load_cases(f)[0]["k"] == 5


def test_recall_bench_cli_uses_snapshot(memory, tmp_path, monkeypatch):
    db, ids = memory
    f = tmp_path / "c.toml"
    f.write_text(f'[[case]]\nquery = "bridge codex"\nexpect = [{ids["bridge"]}]\n')
    out = tmp_path / "r.json"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert cli_main(["recall-bench", str(f), "--db", str(db), "--json", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["summary"]["mrr"] == 1.0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


# ── D10 ─────────────────────────────────────────────────────────────────────


def test_efficiency_per_completed_task(tmp_path):
    db = tmp_path / "room.db"
    Store(db)
    now = time.time()
    con = sqlite3.connect(db)

    def task(mid, recipient, state, usage_rows):
        con.execute("INSERT INTO messages(id, agent, text, sent_at, recipient, kind) VALUES (?,?,?,?,?,?)",
                    (mid, "michal", "t", now - 100, recipient, "task"))
        con.execute("INSERT INTO deliveries(message_id, recipient, state, attempts, updated_at) VALUES (?,?,?,?,?)",
                    (mid, recipient, state, len(usage_rows), now))
        for n, (source, model, tokens, cost) in enumerate(usage_rows, 1):
            con.execute("INSERT INTO task_usage(message_id, recipient, attempt, at, source, model, input_tokens, "
                        "output_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                        (mid, recipient, n, now, source, model, tokens, 0 if tokens is not None else None, cost))

    task(1, "claude-code", "succeeded", [("claude-json", "opus", 1000, 0.10)])
    task(2, "claude-code", "succeeded", [("claude-json", "opus", 500, 0.20), ("claude-json", "opus", 500, 0.10)])
    task(3, "claude-code", "failed", [("claude-json", "opus", 3000, 0.30)])
    task(4, "codex", "succeeded", [("codex-jsonl", "gpt", 800, None)])
    task(5, "codex", "succeeded", [("unreported", None, None, None)])
    con.commit()
    con.close()
    with insight.snapshot(db) as c:
        rep = insight.efficiency(c, now - 3600)
    g = {x["recipient"]: x for x in rep["groups"]}
    cc = g["claude-code"]
    assert cc["task_count"] == 3 and cc["completed"] == 2 and cc["attempts"] == 4
    assert cc["tokens_per_completion"] == 2500 and cc["cost_per_completion"] == pytest.approx(0.35)
    assert cc["small_sample"] is True
    assert g["codex"]["cost_unknown"] == 1 and g["codex"]["cost_per_completion"] is None
    assert rep["unreported"] == 1
    md = insight.efficiency_markdown(rep, 30)
    assert "ne ověřená správnost" in md and "malý vzorek" in md


# ── D11 ─────────────────────────────────────────────────────────────────────


def test_health_finds_known_problems(tmp_path):
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="pkg"\n[project.scripts]\npkg = "pkg.a:main"\n')
    body = "    x = 1\n    y = x + 1\n    z = y * 2\n    if z:\n        z += 1\n    return z\n"
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from pkg import b\n\n\ndef main():\n    return b.used()\n\n\n"
                              f"def first(v):\n{body}\n\ndef unusedThing():\n    return unusedThing()\n\n\n"
                              "def dynamic():\n    return 1\n\n\nHANDLER = getattr(None, 'dynamic', None)\n")
    (pkg / "b.py").write_text(f"from pkg import a\n\n\ndef used():\n    return a.first(1)\n\n\ndef second(w):\n{body}")
    rep = health.analyze(tmp_path)
    assert rep["duplicates"] == [["src/pkg/a.py:8 first", "src/pkg/b.py:8 second"]]
    dead = {d.split(" ")[1] for d in rep["dead_candidates"]}
    assert dead == {"unusedThing", "second"}  # main = entrypoint, dynamic = odkaz v řetězci, used/first volané
    assert rep["import_cycles"] == [["pkg.a", "pkg.b"]]
    assert rep["naming"] == ["src/pkg/a.py:17 unusedThing"]
    assert "kandidáti" in health.markdown(rep)
