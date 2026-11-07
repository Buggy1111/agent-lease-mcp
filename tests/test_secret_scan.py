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
        store.review_verdict("codex", review["id"], "approved", A, security="pass", reviewer_findings=[])
    store.review_verdict("codex", review["id"], "changes_requested", A, "smaž klíč", security="fail")
    clean = store.review_submit("claude-code", review["id"], B, secret_findings=[])
    assert clean["secret_findings"] == [] and clean["security"] is None
    approved = store.review_verdict("codex", review["id"], "approved", B, security="pass", reviewer_findings=[])
    assert approved["security"] == "pass" and approved["security_by"] == "codex"
    assert store.review_approved(str(tmp_path), B)["id"] == review["id"]


def test_unscanned_commit_cannot_be_approved(store, tmp_path):
    review = store.review_open("claude-code", str(tmp_path), "t", "codex", "", A)
    assert review["secret_findings"] is None
    with pytest.raises(PermissionError, match="no secret scan"):
        store.review_verdict("codex", review["id"], "approved", A, security="pass", reviewer_findings=[])
    store.review_submit("claude-code", review["id"], B, secret_findings=[])
    store.review_verdict("codex", review["id"], "approved", B, security="pass", reviewer_findings=[])
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


def test_added_line_that_looks_like_a_header_is_still_scanned():
    sneaky = "+++ " + "sk-" + "z9y8x7w6" * 4  # přidaný řádek „++ sk-…“
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,0 +1,2 @@\n"
            f"{sneaky}\n+ok\n")
    assert scan_diff(diff) == [{"file": "x", "line": 1, "kind": "api-key-sk"}]


def test_hunk_counts_follow_multiple_files():
    diff = ("diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/b b/b\n--- a/b\n+++ b/b\n@@ -5,0 +6 @@\n+key = " + repr("ghp_" + "X1y2Z3" * 6) + "\n")
    assert scan_diff(diff) == [{"file": "b", "line": 6, "kind": "github-token"}]


def test_code_expressions_are_not_secrets():
    """Nox #869: volání funkcí v přiřazení tokenu/hesla nejsou tajemství."""
    eq = " = "  # rozdělené, ať tyhle fixtury nehlásí ani starší (nasazený) scanner
    code = ["+        token_hash" + eq + "hashlib.sha256(token.encode()).hexdigest()",
            "+        token" + eq + "uuid.uuid4().hex",
            "+    token" + eq + "secrets.token_urlsafe(32)",
            "+    password" + eq + "get_password_from_vault(name2)"]
    assert scan_diff(diff_of(code)) == []
    assert scan_diff(diff_of(['+API_TOKEN = "' + "a1B2c3D4e5F6g7H8" + '"']))  # skutečný literál dál ano
    assert scan_diff(diff_of(["+API_TOKEN=" + "a1B2c3D4e5F6g7H8"]))  # i bez uvozovek (env soubor)


def test_quoted_secret_with_parentheses_is_still_found():
    assert scan_diff(diff_of(['+PASSWORD = "' + "ab(c1)d2e3f4g5h6i7" + '"']))
    assert scan_diff(diff_of(["+api_key: '" + "x9(y8)z7w6v5u4t3s2" + "'"]))


def test_long_lines_do_not_backtrack_catastrophically():
    import time
    for line in ["+token=" + "a1" * 50000, "+token = " + "a" * 100000 + "(",
                 '+password = "' + "1a" * 50000, "+" + "token " * 20000 + "= x"]:
        started = time.monotonic()
        scan_diff(diff_of([line]))
        assert time.monotonic() - started < 1.0, line[:40]


def test_long_values_and_aligned_assignments_are_still_found():
    """Nox #9: hodnota delší než okno a víc mezer za = nesmí projít bez nálezu."""
    name = "+API_TO" + "KEN"
    for reps in (256, 257, 5000):
        value = "a1" * reps
        assert scan_diff(diff_of([name + ' = "' + value + '"'])), reps
        assert scan_diff(diff_of([name + "=" + value])), reps
    for spaces in (8, 9, 40):
        assert scan_diff(diff_of([name + " =" + " " * spaces + '"' + "a1B2c3D4e5F6g7H8" + '"'])), spaces
        assert scan_diff(diff_of([name + " " * spaces + "= " + "a1B2c3D4e5F6g7H8"])), spaces
    assert scan_diff(diff_of([name + "=" + "a1B2c3D4e5F6g7H8" + "=="]))  # base64 zarovnání
    assert scan_diff(diff_of(["+" + "x" * 300 + "_api_k" + "ey_" + "y" * 300 + " = " + "a1B2c3D4e5F6g7H8"]))
    assert scan_diff(diff_of(["+PASS" + "WORD=" + "us3r:pa55=w0rd-xyzABC"]))  # i s `:` a `=` uvnitř


def test_shell_command_substitution_is_not_a_secret():
    name = "GITHUB_TO" + "KEN"
    line = f"+export {name}=$(grep \"^{name}=\" ~/.hermes/.env | head -1 | cut -d= -f2 | tr -d '\\n')"
    assert scan_diff(diff_of([line])) == []
    assert scan_diff(diff_of(['+WEBHOOK_SEC' + 'RET="$(openssl rand -hex 32)"'])) == []


def test_repeated_assignments_stay_linear():
    import time
    for line in ["+" + "token=" * 30000 + "(", "+" + "token:" * 30000, "+token=" + " " * 100000 + "(",
                 "+" + 'token="' * 20000,
                 "+" + "token:a1" * 30000 + "(", "+" + "token " * 30000 + "=" + " " * 50000 + "(",
                 "+" + "a" * 200000 + "token" * 20000]:
        started = time.monotonic()
        scan_diff(diff_of([line]))
        assert time.monotonic() - started < 1.0, line[:40]
