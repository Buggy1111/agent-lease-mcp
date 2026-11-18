"""C1b: zápisový běh opravy v izolaci — skutečný bwrap a „zlovolný“ agent (nový C0 pokus na finálním obalu).

Agent je shellový skript, který zkouší vše, co v C0 prošlo nebo by nemělo: zápis mimo
klon (/tmp hostitele, HOME, hlavní repo), změnu .git klonu (config/hooks), čtení stavu
agent-lease (token, DB), symlink ven a jeden legitimní zápis do povoleného souboru.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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
    other = tmp_path / "other-project"         # jiný projekt / soukromá data mimo klon
    other.mkdir()
    (other / "secret.txt").write_text("OTHER-SYNTHETIC")
    import socket as _s
    sock = _s.socket(_s.AF_UNIX)
    sock.bind(str(other / "broker.sock"))            # nestandardní soket (broker mimo ~/.agent-lease)
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
    yield {"tmp": tmp_path, "home": home, "repo": repo, "base": base, "outside": outside, "state": state,
           "other": other}
    sock.close()
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
try read_other    "cat $OTHER/secret.txt | grep -q SYNTHETIC"
try see_socket    "test -e $OTHER/broker.sock"
try read_main     "test -r $REPO/README.md"
echo "\"end\": true}"
} > /dev/shm/probe.json 2>/dev/null || true
cat /dev/shm/probe.json 2>/dev/null
"""


def run_agent(env, clone, script=HOSTILE):
    argv = ["/bin/sh", "-c", script]
    e = {"OUTSIDE": str(env["outside"]), "REPO": str(env["repo"]), "REALHOME": str(env["home"]),
         "STATE": str(env["state"]), "OTHER": str(env["other"])}
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
    assert probe["read_other"] is False and probe["see_socket"] is False     # review #20 F1
    assert probe["read_main"] is False                       # ani hlavní repo není vidět
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


def _bridge_env(env, monkeypatch, body: str, with_tests: bool = True):
    import stat as st

    from agent_lease_mcp.config import Settings
    from agent_lease_mcp.store import Store
    db = env["tmp"] / "bridge-state" / "room.db"
    db.parent.mkdir()
    store = Store(db, settings=Settings(db, "codex", 1800))
    pol = db.parent / "fix-runner.toml"
    tests = json.dumps([sys.executable, "src/test_app.py"]) if with_tests else None
    pol.write_text(f'version = 1\nenabled = true\nrepositories = ["{env["repo"]}"]\nagents = ["codex"]\n'
                   + (f'[tests]\n"{env["repo"]}" = {tests}\n' if tests else ""))
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


def _regression_body(test_code: str) -> str:
    return ("import pathlib\n"
            "pathlib.Path('src/app.py').write_text('print(\"fixed\")\\n')\n"
            f"pathlib.Path('src/test_app.py').write_text({test_code!r})\n"
            "print('synthetic result')\n")


def test_bridge_runs_fix_in_sandbox_and_publishes_branch(env, monkeypatch):
    from agent_lease_mcp.bridge import Bridge
    store, mid, cmd = _bridge_env(env, monkeypatch, (
        "import pathlib\n"
        "pathlib.Path('src/app.py').write_text('print(\"fixed\")\\n')\n"
        "pathlib.Path('src/test_app.py').write_text("
        "'import pathlib,sys\\nsys.exit(0 if \"fixed\" in pathlib.Path(\"src/app.py\").read_text() else 1)\\n')\n"
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


@pytest.mark.parametrize("cancel", [False, True])
def test_cancel_during_verification_prevents_base_test_and_publication(env, monkeypatch, cancel):
    """Review #20/#22: real bwrap reproduction plus the same legitimate control."""
    import threading

    from agent_lease_mcp.bridge import Bridge
    test_code = ('import pathlib,sys,time\ntime.sleep(1)\n'
                 'sys.exit(0 if "fixed" in pathlib.Path("src/app.py").read_text() else 1)\n')
    store, mid, command = _bridge_env(env, monkeypatch, _regression_body(test_code))
    real_verify, real_run = fix_runner.verify_regression, fix_runner.run_sandboxed
    observed, executions = [], []

    def run(clone, *args, **kwargs):
        executions.append(Path(clone).name)
        return real_run(clone, *args, **kwargs)

    def verify(*args, **kwargs):
        if cancel:
            timer = threading.Timer(0.2, lambda: observed.append(store.cancel(mid, "codex")))
            timer.start()
        try:
            return real_verify(*args, **kwargs)
        finally:
            if cancel:
                timer.join()

    monkeypatch.setattr(fix_runner, "verify_regression", verify)
    monkeypatch.setattr(fix_runner, "run_sandboxed", run)
    assert Bridge(store, "codex", command, lease_seconds=30, read_only=False).step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    branches = git("branch", "--list", "agent-fix/*", cwd=env["repo"])
    if cancel:
        assert observed == [True]
        assert job["state"] == "cancelled"
        assert "base-check" not in executions
        assert not branches
    else:
        assert job["state"] == "succeeded"
        assert executions == ["head-check", "base-check"]
        assert branches


@pytest.mark.parametrize("stop", ["cancel", "pause"])
def test_verification_interrupts_running_test(env, monkeypatch, stop):
    import threading
    import time

    from agent_lease_mcp.bridge import Bridge
    code = ('import pathlib,sys,time\n'
            'pathlib.Path("started").write_text("ready")\ntime.sleep(20)\n'
            'pathlib.Path("finished").write_text("done")\n'
            'sys.exit(0 if "fixed" in pathlib.Path("src/app.py").read_text() else 1)\n')
    store, mid, command = _bridge_env(env, monkeypatch, _regression_body(code))
    real_verify, real_cleanup = fix_runner.verify_regression, fix_runner.cleanup
    observed, snapshots = [], []

    def verify(*args, **kwargs):
        workdir = Path(args[5])

        def revoke():
            deadline = time.monotonic() + 5
            while not (workdir / "head-check" / "started").exists():
                if time.monotonic() > deadline:
                    observed.append("head did not start")
                    return
                time.sleep(0.01)
            if stop == "cancel":
                observed.append(store.cancel(mid, "codex"))
            else:
                store.pause("michal")
                observed.append(bool(store.paused()))

        thread = threading.Thread(target=revoke, daemon=True)
        thread.start()
        try:
            return real_verify(*args, **kwargs)
        finally:
            thread.join(8)
            assert not thread.is_alive()

    def cleanup(workdir):
        snapshots.append(((workdir / "head-check" / "finished").exists(),
                          (workdir / "base-check").exists()))
        real_cleanup(workdir)

    monkeypatch.setattr(fix_runner, "verify_regression", verify)
    monkeypatch.setattr(fix_runner, "cleanup", cleanup)
    bridge = Bridge(store, "codex", command, lease_seconds=30, read_only=False)
    started = time.monotonic()
    assert bridge.step()
    assert time.monotonic() - started < 10, "must interrupt the 20-second test"
    assert observed == [True]
    assert snapshots == [(False, False)], "neither finished head nor any base checkout"
    assert not git("branch", "--list", "agent-fix/*", cwd=env["repo"])
    assert not bridge._run_fence.present() and not bridge._cleanup_blocked
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] != "succeeded"


@pytest.mark.parametrize("after", ["head", "verification"])
def test_late_cancellation_never_launches_next_phase(env, monkeypatch, after):
    from agent_lease_mcp.bridge import Bridge
    code = 'import pathlib,sys\nsys.exit(0 if "fixed" in pathlib.Path("src/app.py").read_text() else 1)\n'
    store, mid, command = _bridge_env(env, monkeypatch, _regression_body(code))
    real_run, real_verify = fix_runner.run_sandboxed, fix_runner.verify_regression
    executions = []

    def run(clone, *args, **kwargs):
        executions.append(Path(clone).name)
        result = real_run(clone, *args, **kwargs)
        if after == "head" and Path(clone).name == "head-check":
            assert store.cancel(mid, "codex")
        return result

    def verify(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        if after == "verification":
            assert result["ok"]
            assert store.cancel(mid, "codex")
        return result

    monkeypatch.setattr(fix_runner, "run_sandboxed", run)
    monkeypatch.setattr(fix_runner, "verify_regression", verify)
    assert Bridge(store, "codex", command, lease_seconds=30, read_only=False).step()
    assert executions == (["head-check"] if after == "head" else ["head-check", "base-check"])
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "cancelled"
    assert not git("branch", "--list", "agent-fix/*", cwd=env["repo"])


def test_base_timeout_is_not_a_failing_regression_test(env, monkeypatch):
    from agent_lease_mcp.bridge import Bridge
    code = ('import pathlib,sys,time\n'
            'fixed = "fixed" in pathlib.Path("src/app.py").read_text()\n'
            'time.sleep(0 if fixed else 20)\nsys.exit(0 if fixed else 1)\n')
    store, mid, command = _bridge_env(env, monkeypatch, _regression_body(code))
    monkeypatch.setenv("AGENT_LEASE_FIX_TEST_TIMEOUT", "1")
    bridge = Bridge(store, "codex", command, lease_seconds=30, read_only=False)
    assert bridge.step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "needs_review" and job["last_error"] == "fix: tests not verified"
    assert git("branch", "--list", "agent-fix/*", cwd=env["repo"]), "keep the patch for human review"
    assert not bridge._run_fence.present() and not bridge._cleanup_blocked


def test_verification_cleanup_failure_blocks_publication(env, monkeypatch):
    from agent_lease_mcp import bridge as bridge_mod
    code = 'import pathlib,sys\nsys.exit(0 if "fixed" in pathlib.Path("src/app.py").read_text() else 1)\n'
    store, mid, command = _bridge_env(env, monkeypatch, _regression_body(code))
    real_run = bridge_mod.run_command

    def run(template, *args, **kwargs):
        if not template:  # verification command: simulate an unconfirmed process cleanup
            return bridge_mod.RunResult(False, "cleanup unconfirmed", cleanup_failed=True)
        return real_run(template, *args, **kwargs)

    monkeypatch.setattr(bridge_mod, "run_command", run)
    bridge = bridge_mod.Bridge(store, "codex", command, lease_seconds=30, read_only=False)
    assert bridge.step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "needs_review" and job["last_error"] == "fix: process cleanup not confirmed"
    assert bridge._cleanup_blocked and bridge._run_fence.present()
    assert list((env["tmp"] / "fixes").iterdir()), "retain clone until cleanup is confirmed"
    assert not git("branch", "--list", "agent-fix/*", cwd=env["repo"])


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


@pytest.mark.parametrize("body,reason", [
    ("import pathlib\npathlib.Path('src/app.py').write_text('print(1)\\n')\n", "no regression test"),
    (("import pathlib\npathlib.Path('src/app.py').write_text('print(\"fixed\")\\n')\n"
      "pathlib.Path('src/test_app.py').write_text('import sys\\nsys.exit(0)\\n')\n"), "passes without the fix"),
])
def test_bridge_requires_verified_regression_test(env, monkeypatch, body, reason):
    from agent_lease_mcp.bridge import Bridge
    store, mid, cmd = _bridge_env(env, monkeypatch, body)
    assert Bridge(store, "codex", cmd, lease_seconds=30).step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "needs_review" and job["last_error"] == "fix: tests not verified"
    assert any(reason in m["text"] for m in store.inbox(since_id=0))


def test_bridge_without_test_command_needs_review(env, monkeypatch):
    from agent_lease_mcp.bridge import Bridge
    store, mid, cmd = _bridge_env(env, monkeypatch,
                                  "import pathlib\npathlib.Path('src/app.py').write_text('x\\n')\n",
                                  with_tests=False)
    assert Bridge(store, "codex", cmd, lease_seconds=30).step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "needs_review" and job["last_error"] == "fix: tests not verified"


def test_runtime_paths_never_mount_home_or_its_children(tmp_path, monkeypatch):
    home = Path.home().resolve()
    tool = home / ".cache" / f"al-tool-{os.getpid()}"
    (tool / "bin").mkdir(parents=True, exist_ok=True)
    try:
        exe = tool / "bin" / "x"
        exe.write_text("#!/bin/sh\n")
        _, ro = fix_runner.runtime_paths([str(exe)])
        assert str(tool) in ro and str(home) not in ro and str(home / ".cache") not in ro
        lone = home / ".cache" / f"al-lone-{os.getpid()}"
        lone.write_text("#!/bin/sh\n")
        _, ro = fix_runner.runtime_paths([str(lone)])
        assert ro == [str(lone)]                          # samotný soubor, ne ~/.cache
    finally:
        import shutil
        shutil.rmtree(tool, ignore_errors=True)
        (home / ".cache" / f"al-lone-{os.getpid()}").unlink(missing_ok=True)


def test_verification_uses_commit_not_worktree(env):
    """Agent po commitu podvrhne soubory v pracovní složce — ověření je nesmí vidět."""
    clone = fix_runner.prepare(env["repo"], env["base"], env["tmp"] / "rv")
    (clone / "src" / "app.py").write_text('print("fixed")\n')
    (clone / "src" / "test_app.py").write_text(
        'import pathlib,sys\nsys.exit(0 if "fixed" in pathlib.Path("src/app.py").read_text() else 1)\n')
    res = fix_runner.collect(clone, env["base"], [], "fix")
    assert res["ok"]
    # po commitu: test v pracovní složce „vždy projde“ a ignorovaný podvržený soubor
    (clone / "src" / "test_app.py").write_text("import sys\nsys.exit(0)\n")
    test = [sys.executable, "src/test_app.py"]
    v = fix_runner.verify_regression(clone, env["base"], res["commit"], res["files"], test, env["tmp"] / "rv",
                                     home=env["home"], run_home=None)
    assert v["ok"], v                                   # rozhodl obsah commitu, ne podvrh
