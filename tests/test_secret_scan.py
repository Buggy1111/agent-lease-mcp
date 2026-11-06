"""B6: secret scan diffu a povinný bezpečnostní verdikt před schválením."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_lease_mcp import hygiene
from agent_lease_mcp.secret_scan import scan_diff
from agent_lease_mcp.store import Store

FAKE_SK = "sk-or-v1-" + "a1b2c3d4" * 6
FAKE_GH = "ghp_" + "A1b2C3d4E5" * 4
SAMPLES = {
    "github-token": FAKE_GH,
    "api-key-sk": FAKE_SK,
    "aws-access-key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "slack-token": "xoxb-" + "123456789012-abc",
    "bearer-token": "Authorization: Bearer " + "abcDEF123456ghiJKL789",
    "private-key": "-----BEGIN RSA " + "PRIVATE KEY-----",  # složené, ať scan nehlásí tento test
    "google-api-key": "AIza" + "B" * 35,
    "assigned-secret": "DB_PASS" + 'WORD = "Tr0ub4dor&3xyzQWER"',
}


def diff_of(lines: list[str], path: str = "app/config.py") -> str:
    body = "\n".join(lines)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -10,2 +20,{len(lines)} @@\n{body}\n")


@pytest.mark.parametrize("kind", list(SAMPLES))
def test_each_kind_is_found_in_added_lines(kind):
    found = scan_diff(diff_of([" context", f"+x = {SAMPLES[kind]!r}" if kind != "assigned-secret"
                               else "+" + SAMPLES[kind]]))
    assert found == [{"file": "app/config.py", "line": 21, "kind": kind}]


def test_findings_never_carry_the_secret_and_skip_removed_lines():
    found = scan_diff(diff_of([f"-old = '{FAKE_SK}'", f"+new = '{FAKE_GH}'"]))
    assert found == [{"file": "app/config.py", "line": 20, "kind": "github-token"}]
    assert FAKE_GH not in repr(found) and FAKE_SK not in repr(found)


def test_common_code_is_not_a_secret():
    harmless = ["+token = None", "+password: str = ''", "+TOKEN_ENV = 'AGENT_LEASE_TOKEN'",
                "+def check_password(value):", "+api_key = os.environ['OPENROUTER_API_KEY']",
                "+secret_scan.scan_diff(diff)"]
    assert scan_diff(diff_of(harmless)) == []


def test_this_test_file_does_not_trip_the_scan():
    lines = ["+" + line for line in Path(__file__).read_text().splitlines()]
    assert scan_diff(diff_of(lines, "tests/test_secret_scan.py")) == []


def test_every_redaction_pattern_is_also_scanned():
    for sample in SAMPLES.values():
        if hygiene.redact(sample) != sample:
            assert scan_diff(diff_of([f"+{sample}"])), sample


@pytest.fixture
def store(tmp_path: Path) -> Store:
    (tmp_path / "human-name").write_text("michal")
    return Store(tmp_path / "room.db")


A, B = "a" * 40, "b" * 40


def test_approval_needs_security_pass_and_a_clean_scan(store, tmp_path):
    review = store.review_open("claude-code", str(tmp_path), "t", "codex", "", A,
                               [{"file": "x.py", "line": 3, "kind": "api-key-sk"}])
    assert review["secret_findings"] == [{"file": "x.py", "line": 3, "kind": "api-key-sk"}]
    with pytest.raises(PermissionError, match="security"):
        store.review_verdict("codex", review["id"], "approved", A)
    with pytest.raises(ValueError):
        store.review_verdict("codex", review["id"], "approved", A, security="maybe")
    with pytest.raises(PermissionError, match="1 possible secret"):
        store.review_verdict("codex", review["id"], "approved", A, security="pass")
    store.review_verdict("codex", review["id"], "changes_requested", A, "smaž klíč", security="fail")
    clean = store.review_submit("claude-code", review["id"], B, secret_findings=[])
    assert clean["secret_findings"] == [] and clean["security"] is None
    approved = store.review_verdict("codex", review["id"], "approved", B, security="pass")
    assert approved["security"] == "pass" and approved["security_by"] == "codex"
    assert store.review_approved(str(tmp_path), B)["id"] == review["id"]


def test_unscanned_commit_cannot_be_approved(store, tmp_path):
    review = store.review_open("claude-code", str(tmp_path), "t", "codex", "", A)
    assert review["secret_findings"] is None
    with pytest.raises(PermissionError, match="no secret scan"):
        store.review_verdict("codex", review["id"], "approved", A, security="pass")
    store.review_submit("claude-code", review["id"], B, secret_findings=[])
    store.review_verdict("codex", review["id"], "approved", B, security="pass")
    store.review_submit("claude-code", review["id"], A)  # nový commit bez scanu
    assert store.review_get(review["id"])["secret_findings"] is None


def test_findings_are_validated(store, tmp_path):
    with pytest.raises(ValueError):
        store.review_open("claude-code", str(tmp_path), "t", "codex", "", A, [{"kind": "BAD KIND!"}])
    with pytest.raises(TypeError):
        store.review_open("claude-code", str(tmp_path), "t2", "codex", "", A, ["nope"])


def test_old_reviews_table_gets_the_new_columns(tmp_path):
    db = tmp_path / "room.db"
    Store(db)
    with sqlite3.connect(db) as con:  # simulace DB nasazené před B6
        con.execute("ALTER TABLE reviews DROP COLUMN scan_findings")
        con.execute("ALTER TABLE reviews DROP COLUMN security")
    Store(db)
    with sqlite3.connect(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(reviews)")}
    assert {"scan_commit", "scan_findings", "security", "security_by"} <= cols
