"""
Pravidla se testují bez databáze — proto jsou v `policy.py` čisté funkce.
Tohle je ta část, která se bude měnit nejčastěji (přibude nástroj, upraví se
pokrytí cest), takže musí jít ověřit rychle a bez setupu.
"""

from __future__ import annotations

import time

import pytest

from agent_lease_mcp.models import Claim, Verdict
from agent_lease_mcp.policy import covers, decide, extract_target, normalize_path


def held_by(agent: str, path: str = "/repo/deploy/x.sh", purpose: str = "balení") -> Claim:
    now = time.time()
    return Claim(path=path, agent=agent, purpose=purpose, claimed_at=now, expires_at=now + 600)


class TestCovers:
    def test_exact_path(self):
        assert covers("/repo/a.sh", "/repo/a.sh")

    def test_directory_covers_file_inside(self):
        assert covers("/repo/deploy", "/repo/deploy/make-package.sh")

    def test_directory_with_trailing_slash(self):
        assert covers("/repo/deploy/", "/repo/deploy/make-package.sh")

    def test_sibling_prefix_is_not_covered(self):
        """`/repo/deploy2` není uvnitř `/repo/deploy` — čistý prefix nestačí."""
        assert not covers("/repo/deploy", "/repo/deploy2/x.sh")

    def test_file_does_not_cover_parent(self):
        assert not covers("/repo/deploy/x.sh", "/repo/deploy")


class TestExtractTarget:
    def test_claude_code_shape(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "/repo/a.ts"}}
        assert extract_target(payload) == ("Edit", "/repo/a.ts")

    def test_codex_shape(self):
        payload = {"toolName": "apply_patch", "toolInput": {"path": "/repo/a.ts"}}
        assert extract_target(payload) == ("apply_patch", "/repo/a.ts")

    def test_missing_path(self):
        assert extract_target({"tool_name": "Bash", "tool_input": {"command": "ls"}})[1] is None

    def test_non_dict_input_does_not_crash(self):
        assert extract_target({"tool_name": "Edit", "tool_input": "nesmysl"})[1] is None


class TestDecide:
    def test_unguarded_tool_passes(self):
        d = decide("Bash", "/repo/a.sh", held_by("codex", "/repo/a.sh"), me="claude-code")
        assert d.verdict is Verdict.ALLOW

    def test_free_path_is_auto_claimed(self):
        d = decide("Edit", "/repo/a.sh", None, me="claude-code")
        assert d.verdict is Verdict.AUTO_CLAIMED

    def test_own_claim_passes(self):
        d = decide("Edit", "/repo/a.sh", held_by("claude-code", "/repo/a.sh"), me="claude-code")
        assert d.verdict is Verdict.ALLOW

    def test_foreign_claim_blocks(self):
        d = decide("Edit", "/repo/a.sh", held_by("codex", "/repo/a.sh"), me="claude-code")
        assert d.blocks

    def test_block_reason_names_who_and_why(self):
        """Hláška musí agentovi říct, s kým se má domluvit — jinak jen hádá."""
        d = decide("Edit", "/repo/a.sh", held_by("codex", "/repo/a.sh", "balicí skript"), "claude-code")
        assert "codex" in d.reason
        assert "balicí skript" in d.reason

    def test_paths_are_normalized(self, tmp_path, monkeypatch):
        (tmp_path / "a.sh").write_text("x")
        monkeypatch.chdir(tmp_path)
        d = decide("Edit", "a.sh", None, me="claude-code")
        assert d.path == normalize_path(str(tmp_path / "a.sh"))


class TestShellWriteTargets:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo x > a.txt", ["a.txt"]),
            ("echo x>>b.log 2>&1", ["b.log"]),
            ("cat a | tee -a c.txt d.txt", ["c.txt", "d.txt"]),
            ("sed -i 's/a/b/' e.py f.py", ["e.py", "f.py"]),
            ("cp src.txt dst.txt", ["dst.txt"]),
            ("ls > /dev/null", []),
            ("git commit -m 'a > b'", []),
            ("python x.py", []),
            ("sudo tee f.txt", ["f.txt"]),
            ("env A=1 B=2 tee g.txt", ["g.txt"]),
            ("bash -c 'echo x > i.txt'", ["i.txt"]),
            ("nohup python x.py > k.log", ["k.log"]),
        ],
    )
    def test_recognised_shapes(self, command, expected):
        from agent_lease_mcp.policy import shell_write_targets

        assert shell_write_targets(command) == expected


def test_script_target_sees_through_wrappers(tmp_path, monkeypatch):
    from agent_lease_mcp.policy import script_target

    (tmp_path / "x.sh").write_text("echo")
    monkeypatch.chdir(tmp_path)
    assert script_target("sudo bash x.sh") == "x.sh"
    assert script_target("env A=1 bash x.sh") == "x.sh"
