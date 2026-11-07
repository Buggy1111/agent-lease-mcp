"""Real offline Python/TypeScript pipelines, explicitly enabled inside a bounded scope.

No compiler/exit-code/namespace mocks. All rooms, roles and reviews are synthetic;
these tests do not constitute independent review of Nox's implementation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from test_quality import commit, git
from test_quality_runner import write_policy
from test_webui import authenticated_handler, call_handler

from agent_lease_mcp.broker import RemoteStore, serve
from agent_lease_mcp.config import Settings
from agent_lease_mcp.quality import inspect_repository
from agent_lease_mcp.quality_runner import verify_limits
from agent_lease_mcp.store import Store

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_LEASE_QUALITY_E2E") != "1",
                               reason="requires explicitly enabled bounded offline sandbox E2E")


@pytest.fixture(params=["python", "typescript"])
def project(request, tmp_path):
    verify_limits()  # fail, never skip silently, if the claimed environment is absent
    root = tmp_path / "project"
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    git(root, "config", "user.name", "E2E fixture")
    git(root, "config", "user.email", "e2e@example.invalid")
    commit(root, {"README.md": "fixture"})
    store = Store(settings=Settings(tmp_path / "room" / "room.db", "codex", 1800))
    tools = {"python": str(Path("/usr/bin/python3").resolve())}
    mounts = {}
    if request.param == "python":
        files = {"pyproject.toml": '[project]\nname="fixture"\nversion="1"\n',
                 ".agent-lease/quality.toml": '''version=1
language="python"
[checks.tests]
command=["python","-m","unittest","discover","-s","tests","-v"]
[checks.lint]
command=["python","lint.py"]
[checks.build]
command=["python","-m","compileall","-q","src"]
''',
                 "tests/test_app.py": "import unittest\nfrom src.app import answer\nclass App(unittest.TestCase):\n def test_answer(self): self.assertEqual(answer,42)\n",
                 "lint.py": "import ast,pathlib\nfor p in pathlib.Path('src').glob('*.py'): ast.parse(p.read_text()); assert '\\t' not in p.read_text()\n",
                 "src/app.py": "answer = 41\n"}
        good = {"src/app.py": "answer = 42\n"}
    else:
        compiler = Path(os.environ["AGENT_LEASE_E2E_TYPESCRIPT"]).resolve()
        mounts["typescript"] = compiler
        tools.update(node=str(Path("/usr/bin/node").resolve()), npm=str(Path("/usr/bin/npm").resolve()),
                     tsc=str(compiler / "bin" / "tsc"))
        files = {"package.json": json.dumps({"name":"fixture","private":True,"packageManager":"npm@9.2.0",
                 "devDependencies":{"typescript":"5.9.3"},"scripts":{
                 "test":"tsc --outDir /tmp/compiled && node --test test.cjs",
                 "lint":"node lint.cjs", "typecheck":"tsc --noEmit", "build":"tsc --outDir dist"}}),
                 "tsconfig.json": '{"compilerOptions":{"strict":true,"target":"ES2022","module":"commonjs","skipLibCheck":true},"include":["src/**/*.ts"]}',
                 "test.cjs": "const t=require('node:test'),a=require('node:assert/strict');t('answer',()=>a.equal(require('/tmp/compiled/app.js').answer,42));\n",
                 "lint.cjs": "const fs=require('node:fs'),a=require('node:assert/strict');a.ok(!fs.readFileSync('src/app.ts','utf8').includes('\\t'));\n",
                 "src/app.ts": "export const answer: number = 41;\n"}
        good = {"src/app.ts": "export const answer: number = 42;\n"}
    commit(root, files)
    head = commit(root, good)
    write_policy(store, root, mounts=mounts, tools=tools,
                 timeout=30 if request.param == "typescript" else 10)
    return root, store, request.param, head


def approve(store, root, head, actor="codex"):
    plan = inspect_repository(root, head)
    peer = "claude-code" if actor == "codex" else "codex"
    review = store.review_open(actor, str(root), "fixture-"+uuid.uuid4().hex, peer,
                               plan.base_commit, head, secret_findings=[])
    store.review_verdict(peer, review["id"], "approved", head,
                         security="pass", reviewer_findings=[])


@pytest.mark.parametrize("actor", ["claude-code", "codex"])
def test_real_pass_and_fail_for_each_agent(project, actor):
    root, store, language, head = project
    approve(store, root, head, actor)
    good = store.quality_check(actor, str(root), head)
    assert good["kind"] == "quality_run", json.dumps(good)
    assert good["overall_status"] == "PASS", good
    assert good["branch"] == "main"
    assert good["checks"]["coverage"]["status"] == "NA"
    assert good["sandbox"]["limits"]["memory_bytes"] <= 2 * 1024**3
    if language == "typescript":
        assert good["checks"]["typecheck"]["status"] == "PASS"
    name = "src/app.py" if language == "python" else "src/app.ts"
    bad = commit(root, {name: "answer=0\n" if language == "python" else 'export const answer: number = "wrong";\n'})
    result = store.quality_check(actor, str(root), bad)
    assert result["overall_status"] == "FAIL", result
    assert result["checks"]["tests"]["status"] == "FAIL"
    if language == "typescript":
        assert result["checks"]["typecheck"]["status"] == "FAIL"


def test_both_agents_and_existing_broker_snapshot_history(project, tmp_path):
    root, store, _, head = project
    approve(store, root, head)
    sock = tmp_path / "quality.sock"
    server = serve(store.settings, sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        remote = RemoteStore(store.settings, sock)
        first = remote.quality_check("codex", str(root), head)
        second = remote.quality_check("claude-code", str(root), head)
        assert first["overall_status"] == second["overall_status"] == "PASS", (first, second)
        assert remote.quality_runs()[0]["history"][0]["run_id"] == first["run_id"]
        handler = authenticated_handler(store, store.settings, "fixture-only")
        status, body = call_handler(store, store.settings, "fixture-only", "GET", "/api/snapshot",
            headers={"Cookie":"agent_lease_session=session"}, handler_type=handler)
        assert status == 200 and json.loads(body)["quality_runs"][0]["run_id"] == second["run_id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_missing_review_and_changed_tests_remain_review(project):
    root, store, language, head = project
    pending = store.quality_check("codex", str(root), head)
    assert pending["overall_status"] == "REVIEW", pending
    filename = "tests/test_app.py" if language == "python" else "test.cjs"
    changed = commit(root, {filename: "import unittest\nclass Weakened(unittest.TestCase):\n def test_answer(self): self.assertTrue(True)\n" if language == "python" else "// tests weakened\n"})
    approve(store, root, changed)
    result = store.quality_check("codex", str(root), changed)
    assert result["overall_status"] == "REVIEW" and result["policy_review_required"], result
    approved = store.quality_human_approve_policy("michal", result["run_id"], changed,
                                                 "Synthetic human accepted the critical fixture change.")
    assert approved["overall_status"] == "PASS" and approved["policy_review_status"] == "approved"
    # The exception is scoped to the exact run, never a future run or commit.
    repeated = store.quality_check("codex", str(root), changed)
    assert repeated["overall_status"] == "REVIEW"


def test_dirty_checkout_is_never_executed(project):
    root, store, language, head = project
    approve(store, root, head)
    file = root / ("src/app.py" if language == "python" else "src/app.ts")
    file.write_text("MUST NOT EXECUTE THIS DIRTY CONTENT\n")
    result = store.quality_check("codex", str(root), head)
    assert result["overall_status"] == "PASS", result
    assert file.read_text().startswith("MUST NOT EXECUTE")


def test_valid_task_binding_and_no_implicit_completion(project):
    root, store, _, head = project
    approve(store, root, head)
    mid = store.send("michal", "codex", "fixture quality task", kind="task", project=str(root))
    delivery = store.lease_next("codex", lease_seconds=300, start=True)
    assert delivery.message_id == mid
    result = store.quality_check("codex", str(root), head, task_id=mid, lease_token=delivery.lease_token)
    assert result["task_id"] == mid and result["overall_status"] == "PASS", result
    assert store.holds(mid, "codex", delivery.lease_token)
    wrong = store.quality_check("claude-code", str(root), head, task_id=mid, lease_token=delivery.lease_token)
    assert not wrong["executed"]


def test_sandbox_hides_home_environment_network_and_raw_output(project, tmp_path, monkeypatch):
    root, store, _, _ = project
    secret = tmp_path / "private-marker"
    secret.write_text("fixture-private-data")
    monkeypatch.setenv("B7_PRIVATE_TEST_VALUE", "fixture-host-environment")
    script = f'''import os,pathlib,socket,threading
assert not pathlib.Path({str(secret)!r}).exists()
assert 'B7_PRIVATE_TEST_VALUE' not in os.environ
assert not pathlib.Path('/home/buggy1111/.agent-lease').exists()
assert not pathlib.Path('/sys/fs/cgroup').exists()
assert int(next(line.split()[1] for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:')),16)==0
for fd in range(3,65):
 try:
  leaked=os.open('/proc/1/fd/'+str(fd),os.O_RDWR)
 except OSError:
  continue
 else:
  os.close(leaked);raise AssertionError('trusted collector descriptor escaped')
os.environ.pop('HOME',None)
assert pathlib.Path.home()==pathlib.Path('/tmp/home')
server=socket.socket();server.bind(('127.0.0.1',0));server.listen()
def reply():
 conn,_=server.accept();conn.sendall(b'loopback-ok');conn.close()
thread=threading.Thread(target=reply);thread.start()
client=socket.create_connection(server.getsockname(),timeout=1)
assert client.recv(64)==b'loopback-ok';client.close();thread.join();server.close()
try:
 socket.create_connection(('192.0.2.1',443),timeout=.2)
except OSError:
 pass
else:
 raise AssertionError('network escaped')
try:
 pathlib.Path('/usr/SHOULD_NOT_WRITE').write_text('bad')
except OSError:
 pass
else:
 raise AssertionError('read-only mount escaped')
print('fixture-private-output-to-withhold')
'''
    head = commit(root, {"sandbox_probe.py":script,".agent-lease/quality.toml":'''version=1
language="python"
[checks.tests]
command=["python","sandbox_probe.py"]
[checks.lint]
command=["python","sandbox_probe.py"]
[checks.build]
command=["python","sandbox_probe.py"]
''',"pyproject.toml":'[project]\nname="probe"\n'})
    result = store.quality_check("codex", str(root), head)
    assert result["checks"]["tests"]["status"] == "PASS", result
    log = store.quality_log(result["run_id"], "tests")
    assert "fixture-private-output-to-withhold" not in log
    assert "fixture-private-data" not in json.dumps(result)


def test_timeout_and_output_limit_are_bounded(project):
    root, store, _, _ = project
    head = commit(root, {"pyproject.toml":'[project]\nname="bounds"\n',
                        ".agent-lease/quality.toml":'''version=1
language="python"
[checks.tests]
command=["python","-c","import time; time.sleep(30)"]
'''})
    policy = store.db_path.parent / "quality-runner.toml"
    policy.write_text(re.sub(r"timeout_seconds=\d+", "timeout_seconds=1", policy.read_text()))
    started = time.monotonic()
    result = store.quality_check("codex", str(root), head)
    assert result["checks"]["tests"]["status"] == "FAIL", result
    assert result["observations"][0]["timed_out"] and time.monotonic()-started < 8
    head = commit(root, {".agent-lease/quality.toml":'''version=1
language="python"
[checks.tests]
command=["python","-c","import sys; sys.stdout.buffer.write(b'x'*2000000)"]
'''})
    result = store.quality_check("codex", str(root), head)
    assert result["overall_status"] == "REVIEW", result
    assert result["observations"][0]["output_limited"]
    assert result["observations"][0]["output_bytes"] <= 1024**2 + 1


def matching_processes(marker):
    found = []
    for process in Path("/proc").iterdir():
        if process.name.isdigit():
            try:
                if marker.encode() in (process / "cmdline").read_bytes():
                    found.append(int(process.name))
            except OSError:
                pass
    return found


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_pause_or_cancellation_kills_running_namespace_and_descendants(project, action):
    root, store, _, _ = project
    marker = "quality-child-" + uuid.uuid4().hex
    head = commit(root, {"pyproject.toml":'[project]\nname="stop"\n',
                        "stop_probe.py":f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(30)',{marker!r}],start_new_session=True)\ntime.sleep(30)\n",
                        ".agent-lease/quality.toml":'''version=1
language="python"
[checks.tests]
command=["python","stop_probe.py"]
'''})
    mid = store.send("michal", "codex", "fixture stop task", kind="task", project=str(root))
    delivery = store.lease_next("codex", lease_seconds=300, start=True)
    results = []
    thread = threading.Thread(target=lambda: results.append(store.quality_check(
        "codex", str(root), head, task_id=mid, lease_token=delivery.lease_token)))
    thread.start()
    deadline = time.monotonic() + 8
    while not matching_processes(marker) and time.monotonic() < deadline:
        time.sleep(.05)
    assert matching_processes(marker), "project descendant never started"
    if action == "pause":
        store.pause("michal", "fixture stop")
    else:
        store.cancel(mid, "codex", actor="michal")
    thread.join(8)
    assert not thread.is_alive() and results, "supervisor failed to stop promptly"
    assert results[0]["overall_status"] == "REVIEW", results
    assert results[0]["observations"][0]["interrupted"]
    deadline = time.monotonic() + 2
    while matching_processes(marker) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not matching_processes(marker), "setsid descendant survived namespace cleanup"


def test_background_descendant_does_not_survive_success(project):
    root, store, _, _ = project
    marker = "quality-daemon-" + uuid.uuid4().hex
    head = commit(root, {"pyproject.toml":'[project]\nname="daemon"\n',
                        "daemon_probe.py":f"import subprocess,sys\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(30)',{marker!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n",
                        ".agent-lease/quality.toml":'''version=1
language="python"
[checks.tests]
command=["python","daemon_probe.py"]
'''})
    result = store.quality_check("codex", str(root), head)
    assert result["checks"]["tests"]["status"] == "PASS", result
    assert not matching_processes(marker)


def test_global_stop_hook_enqueues_and_broker_runs_without_model_tokens(project, tmp_path):
    root, store, _, head = project
    approve(store, root, head)
    policy = store.db_path.parent / "quality-runner.toml"
    policy.write_text(policy.read_text().replace("enabled=true", "enabled=true\nautomatic=true"))
    sock = tmp_path / "auto.sock"
    server = serve(store.settings, sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ, AGENT_NAME="codex", AGENT_LEASE_DB=str(store.db_path),
                   AGENT_LEASE_SOCKET=str(sock), AGENT_LEASE_REQUIRE_BROKER="1",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        env.pop("AGENT_LEASE_QUALITY_AUTORUN", None)
        started = time.monotonic()
        hook = subprocess.run([sys.executable, "-c", "from agent_lease_mcp.lifecycle import release_main; raise SystemExit(release_main())"],
                              cwd=root, env=env, capture_output=True, timeout=10, check=False)
        assert hook.returncode == 0 and time.monotonic()-started < 5, hook.stderr
        deadline = time.monotonic() + 130
        while time.monotonic() < deadline:
            rows = store.quality_requests()
            if rows and rows[0]["state"] not in {"queued", "running"}:
                break
            time.sleep(.1)
        assert rows[0]["state"] == "completed", rows
        assert store.quality_get(rows[0]["run_id"])["overall_status"] == "PASS"
        # Same clean commit after another turn never reruns automatically.
        before = rows[0]["id"]
        again = subprocess.run([sys.executable, "-c", "from agent_lease_mcp.lifecycle import release_main; raise SystemExit(release_main())"],
                               cwd=root, env=env, capture_output=True, timeout=10, check=False)
        assert again.returncode == 0 and store.quality_requests()[0]["id"] == before
        cli = subprocess.run([sys.executable, "-c", "from agent_lease_mcp.cli import main; raise SystemExit(main())",
                              "quality", "check", "--repo", str(root), "--commit", head],
                             cwd=root, env=env, capture_output=True, timeout=150, check=False)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["overall_status"] == "PASS"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_supervisor_sigkill_kills_even_setsid_descendants(project):
    root, store, _, _ = project
    marker = "quality-owner-death-" + uuid.uuid4().hex
    head = commit(root, {"pyproject.toml":'[project]\nname="owner-death"\n',
        "death_probe.py":f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(30)',{marker!r}],start_new_session=True)\ntime.sleep(30)\n",
        ".agent-lease/quality.toml":'''version=1
language="python"
[checks.tests]
command=["python","death_probe.py"]
'''})
    env = dict(os.environ, AGENT_NAME="codex", AGENT_LEASE_DB=str(store.db_path),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    script = "from agent_lease_mcp.store import Store; from agent_lease_mcp.config import Settings; Store(settings=Settings.from_env()).quality_check('codex'," + repr(str(root)) + "," + repr(head) + ")"
    owner = subprocess.Popen([sys.executable,"-c",script], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+8
        while not matching_processes(marker) and time.monotonic()<deadline:
            time.sleep(.05)
        assert matching_processes(marker), "project descendant never started"
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic()+3
        while matching_processes(marker) and time.monotonic()<deadline:
            time.sleep(.05)
        assert not matching_processes(marker), "parent-death cleanup left an escaped child"
        assert store.quality_runs() == []
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)


def test_real_numeric_coverage_and_report_only_crap(project):
    root, store, language, _ = project
    if language == "python":
        deps = Path(os.environ["AGENT_LEASE_E2E_METRICS"]).resolve()
        config = (root / ".agent-lease/quality.toml").read_text()
        config += '\n[checks.coverage]\ncommand=["python","coverage_pipeline.py"]\n'
        commit(root, {".agent-lease/quality.toml":config,
            "coverage_pipeline.py": "import coverage,unittest\nc=coverage.Coverage(source=['src'],timid=True)\nc.start()\nr=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests'))\nc.stop()\nc.json_report(outfile='coverage.json')\nraise SystemExit(not r.wasSuccessful())\n"})
        write_policy(store, root, mounts={"site":deps}, timeout=30)
        head = commit(root, {"src/app.py":"answer = 42\ndef unused(x):\n" + ''.join(
            f" if x == {i}: return {i}\n" for i in range(8)) + " return -1\n"})
        approve(store, root, head)
        run = store.quality_check("codex", str(root), head)
        assert run["checks"]["coverage"]["status"] == "REVIEW", run
        assert run["checks"]["crap"]["status"] == "REVIEW", run
        assert "1 REVIEW" in run["checks"]["crap"]["value"], run
        assert run["overall_status"] == "REVIEW", run
    else:
        package = json.loads((root / "package.json").read_text())
        package["scripts"]["coverage"] = "tsc --outDir /tmp/compiled && node coverage.cjs"
        commit(root, {"package.json":json.dumps(package), "coverage.cjs": '''const fs=require('node:fs'),vm=require('node:vm');
const instrument=require('/usr/share/nodejs/istanbul-lib-instrument').createInstrumenter();
const file='/tmp/compiled/app.js';
const box={exports:{}};
vm.runInNewContext(instrument.instrumentSync(fs.readFileSync(file,'utf8'),file),box);
require('node:assert/strict').equal(box.exports.answer,42);
const map=require('/usr/share/nodejs/istanbul-lib-coverage').createCoverageMap(box.__coverage__);
fs.mkdirSync('coverage',{recursive:true});
fs.writeFileSync('coverage/coverage-summary.json',JSON.stringify({total:map.getCoverageSummary().toJSON()}));
'''})
        head = commit(root, {"src/app.ts":"export const answer: number = 42; // measured JS output\n"})
        approve(store, root, head)
        run = store.quality_check("codex", str(root), head)
        assert run["checks"]["coverage"]["status"] == "PASS", run
        assert run["checks"]["crap"]["status"] == "NA", run
        assert run["overall_status"] == "PASS", run
    assert "% lines" in run["checks"]["coverage"]["value"]
    assert "coverage_pipeline" not in json.dumps(run.get("observations"))
