"""C1b: zápisový běh opravy v izolaci — skutečný bwrap a „zlovolný“ agent (nový C0 pokus na finálním obalu).

Agent je shellový skript, který zkouší vše, co v C0 prošlo nebo by nemělo: zápis mimo
klon (/tmp hostitele, HOME, hlavní repo), změnu .git klonu (config/hooks), čtení stavu
agent-lease (token, DB), symlink ven a jeden legitimní zápis do povoleného souboru.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_lease_mcp import fix_runner

pytestmark = pytest.mark.skipif(not Path(fix_runner.BWRAP).exists(), reason="bubblewrap není k dispozici")


def git(*a: str, cwd: Path) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path: Path):
    # Mimo /tmp: uvnitř sandboxu je /tmp soukromý tmpfs, takže by zápis „prošel“ jen do kopie
    # a test by nedokazoval, že HOME a repo jsou opravdu jen pro čtení.
    import shutil
    import uuid
    root = Path.home() / ".cache" / "agent-lease-tests" / uuid.uuid4().hex
    root.mkdir(parents=True)
    tmp_path = root
    home = tmp_path / "home"
    (home / ".agent-lease").mkdir(parents=True)
    (home / ".agent-lease" / "webui.token").write_text("TOKEN-SYNTHETIC")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_test").write_text("KEY-SYNTHETIC")
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text('{"token": "old"}')
    (home / ".codex" / "config.toml").write_text("model = 'x'\n")
    state = tmp_path / "state"                  # stav místnosti mimo HOME (AGENT_LEASE_DB)
    state.mkdir()
    (state / "webui.token").write_text("STATE-SYNTHETIC")
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('bug')\n")
    (repo / "README.md").write_text("readme\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    base = git("rev-parse", "HEAD", cwd=repo)
    outside = Path("/tmp") / f"agent-lease-host-marker-{uuid.uuid4().hex}"
    yield {"tmp": tmp_path, "home": home, "repo": repo, "base": base, "outside": outside, "state": state}
    shutil.rmtree(root, ignore_errors=True)
    outside.unlink(missing_ok=True)


HOSTILE = r"""
set +e
r=probe.json; : > /dev/null
try(){ if sh -c "$2" 2>/dev/null; then echo "\"$1\": true," ; else echo "\"$1\": false,"; fi; }
{
echo "{"
try legit_write   "printf 'print(\"fixed\")\n' > src/app.py"
try host_tmp      "echo x > $OUTSIDE"
try private_tmp   "echo x > /tmp/scratch"
try home_write    "echo x > $REALHOME/evil"
try main_repo     "echo x > $REPO/README.md"
try git_config    "echo '[core]' >> .git/config"
try git_hook      "echo 'exit 0' > .git/hooks/post-commit"
try read_token    "cat $HOME/.agent-lease/webui.token | grep -q SYNTHETIC"
try read_ssh      "cat $HOME/.ssh/id_test | grep -q SYNTHETIC"
try cli_config    "echo 'mcp = evil' >> $REALHOME/.codex/config.toml"
try run_home_auth "grep -q old $HOME/.codex/auth.json && echo '{\"token\": \"new\"}' > $HOME/.codex/auth.json"
try read_state    "cat $STATE/webui.token | grep -q SYNTHETIC"
try env_leak      "env | grep -q AGENT_LEASE_SECRET"
echo "\"end\": true}"
} > /dev/shm/probe.json 2>/dev/null || true
cat /dev/shm/probe.json 2>/dev/null
"""


def run_agent(env, clone, script=HOSTILE):
    argv = ["/bin/sh", "-c", script]
    e = {"OUTSIDE": str(env["outside"]), "REPO": str(env["repo"]), "REALHOME": str(env["home"]),
         "STATE": str(env["state"])}
    run_home = clone.parent / "home"
    if not run_home.exists():
        fix_runner.prepare_home(clone.parent, env["home"], (".codex/auth.json", ".codex/config.toml"))
    host_env = {"AGENT_LEASE_SECRET": "x", "LANG": "C.UTF-8"}
    full = fix_runner.sandbox_argv(clone, argv, home=env["home"], run_home=run_home,
                                   hide_abs=(str(env["state"]),), env=host_env)
    # proměnné předáme přes --setenv před příkazem
    i = full.index("--")
    for k, v in e.items():
        full[i:i] = ["--setenv", k, v]
        i += 3
    return subprocess.run(full, capture_output=True, text=True, timeout=60, check=False)


def test_hostile_agent_is_contained(env):
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "run1")
    p = run_agent(env, clone)
    probe = json.loads(p.stdout)
    assert probe["legit_write"] is True                      # povolený soubor v klonu jde
    assert probe["private_tmp"] is True                      # soukromý /tmp jde…
    assert not env["outside"].exists()                       # …ale do /tmp hostitele se nic nedostane
    assert probe["home_write"] is False and not (env["home"] / "evil").exists()
    assert probe["main_repo"] is False and (env["repo"] / "README.md").read_text() == "readme\n"
    assert probe["git_config"] is False and probe["git_hook"] is False   # .git klonu jen pro čtení
    assert probe["read_token"] is False and probe["read_ssh"] is False   # stav místnosti a klíče skryté
    assert probe["cli_config"] is False and "evil" not in (env["home"] / ".codex" / "config.toml").read_text()
    assert probe["run_home_auth"] is True                    # CLI si obnoví přihlášení v kopii…
    assert fix_runner.sync_credentials(clone.parent / "home", env["home"], (".codex/auth.json",)) == [".codex/auth.json"]
    assert "new" in (env["home"] / ".codex" / "auth.json").read_text()   # …a zpět jde jen auth.json
    assert probe["read_state"] is False and probe["env_leak"] is False
    assert git("rev-parse", "HEAD", cwd=env["repo"]) == env["base"]


def test_collect_commits_allowed_change_and_publishes_branch(env):
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "run2")
    run_agent(env, clone, "printf 'print(\"fixed\")\\n' > src/app.py")
    res = fix_runner.collect(clone, env["base"], ["src"], "fix(#1): oprava app")
    assert res["ok"] and res["files"] == ["src/app.py"] and not res["rejected"]
    head = fix_runner.publish(env["repo"], clone, res["commit"], "agent-fix/1")
    assert head == res["commit"]
    assert git("rev-parse", "main", cwd=env["repo"]) == env["base"]          # main nedotčený
    assert git("log", "-1", "--format=%an", "agent-fix/1", cwd=env["repo"]) == "agent-lease"
    assert git("show", "agent-fix/1:src/app.py", cwd=env["repo"]) == 'print("fixed")'


@pytest.mark.parametrize("script,reason", [
    ("echo x > README.md", "outside allowed paths"),
    ("mkdir -p .github/workflows && echo x > .github/workflows/ci.yml", "critical file"),
    ("echo '* filter=evil' > src/.gitattributes", "critical file"),
    ("mkdir -p src/.GitHub && echo x > src/.GitHub/x.yml", "critical file"),
    ("printf 'a\\000b' > src/blob.bin", "binary file"),
    ("ln -s /etc/passwd src/link", "symlink"),
])
def test_collect_rejects(env, script, reason):
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / ("r" + str(abs(hash(script)))))
    run_agent(env, clone, script)
    allowed = ["src"] if reason == "outside allowed paths" else []
    res = fix_runner.collect(clone, env["base"], allowed, "fix")
    assert not res["ok"] and any(reason in r["reason"] for r in res["rejected"]), res


def test_collect_rejects_secret(env):
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "run4")
    run_agent(env, clone, "printf 'API_KEY = \"sk-%s\"\\n' abcdefghijklmnopqrstuvwxyz123456 > src/conf.py")
    res = fix_runner.collect(clone, env["base"], [], "fix")
    assert not res["ok"] and res["findings"]


def test_no_changes_is_not_ok(env):
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "run5")
    res = fix_runner.collect(clone, env["base"], [], "fix")
    assert not res["ok"] and res["rejected"][0]["reason"] == "no changes"


def test_publish_rejects_other_branches(env):
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "run6")
    with pytest.raises(ValueError):
        fix_runner.publish(env["repo"], clone, env["base"], "main")


def test_prepare_refuses_existing_workdir(env):
    (env["tmp"] / "busy").mkdir()
    with pytest.raises(FileExistsError):
        fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "busy")


def test_hooks_never_run_in_trusted_process(env):
    """Hook v hlavním repu (např. podvržený) se při klonu/commitu/fetch nespustí."""
    marker = env["tmp"] / "hook-ran"
    hook = env["repo"] / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hook, 0o755)
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "run7")
    run_agent(env, clone, "printf 'x\\n' > src/new.py")
    res = fix_runner.collect(clone, env["base"], [], "fix")
    fix_runner.publish(env["repo"], clone, res["commit"], "agent-fix/7")
    assert not marker.exists()


# ── C1c: bridge spustí opravu v izolovaném klonu a předá větev ─────────────────
def test_sync_credentials_ignores_invalid(env, tmp_path=None):
    run_home = env["tmp"] / "rh"
    (run_home / ".codex").mkdir(parents=True)
    (run_home / ".codex" / "auth.json").write_text("not json")
    assert fix_runner.sync_credentials(run_home, env["home"], (".codex/auth.json",)) == []
    assert "old" in (env["home"] / ".codex" / "auth.json").read_text()


def _bridge_env(env, monkeypatch, body: str):
    import stat as st
    import sys

    from agent_lease_mcp.config import Settings
    from agent_lease_mcp.store import Store
    db = env["tmp"] / "bridge-state" / "room.db"
    db.parent.mkdir()
    store = Store(db, settings=Settings(db, "codex", 1800))
    pol = db.parent / "fix-runner.toml"
    pol.write_text(f'version = 1\nenabled = true\nrepositories = ["{env["repo"]}"]\nagents = ["codex"]\n')
    os.chmod(pol, 0o600)
    script = env["tmp"] / "agent.py"
    script.write_text(body)
    script.chmod(script.stat().st_mode | st.S_IEXEC)
    monkeypatch.setenv("AGENT_LEASE_FIX_DIR", str(env["tmp"] / "fixes"))
    monkeypatch.setenv("AGENT_LEASE_FIX_CLI_FILES", "")
    mid = store.send("michal", "codex", "oprav app.py", kind="task", fix_repo=str(env["repo"]),
                     fix_base=env["base"], fix_allowed=["src"])
    assert store.approve_fix(mid, "codex", "michal", "test")
    return store, mid, f"{sys.executable} {script} {{prompt}}"


def test_bridge_runs_fix_in_sandbox_and_publishes_branch(env, monkeypatch):
    from agent_lease_mcp.bridge import Bridge
    store, mid, cmd = _bridge_env(env, monkeypatch, (
        "import pathlib\n"
        "pathlib.Path('src/app.py').write_text('print(\"fixed\")\\n')\n"
        "try:\n    pathlib.Path('" + str(env["repo"]) + "/README.md').write_text('pwned')\nexcept OSError:\n    pass\n"
        "print('hotovo')\n"))
    assert Bridge(store, "codex", cmd, lease_seconds=30).step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "succeeded", job
    branches = git("branch", "--list", "agent-fix/*", cwd=env["repo"])
    assert f"agent-fix/{mid}-" in branches
    assert (env["repo"] / "README.md").read_text() == "readme\n"          # hlavní repo nedotčené
    assert git("rev-parse", "main", cwd=env["repo"]) == env["base"]
    assert not list((env["tmp"] / "fixes").iterdir())                    # klon po úspěchu uklizen


def test_bridge_rejected_change_goes_to_review(env, monkeypatch):
    from agent_lease_mcp.bridge import Bridge
    store, mid, cmd = _bridge_env(env, monkeypatch,
                                  "import pathlib\npathlib.Path('README.md').write_text('x')\n")
    assert Bridge(store, "codex", cmd, lease_seconds=30).step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "needs_review" and job["last_error"] == "fix: changes rejected"
    assert any("outside allowed paths" in m["text"] for m in store.inbox(since_id=0) if m.get("reply_to") == mid
               or f"#{mid}" in m["text"])
    assert not git("branch", "--list", "agent-fix/*", cwd=env["repo"])
