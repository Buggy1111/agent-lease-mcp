"""Trusted scope launcher: verify limits, then use the exact reviewed sandbox wrapper."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
unit, phase, *command = sys.argv[1:]
out = ROOT / 'pilot/out' / phase
out.mkdir(parents=True, exist_ok=True)
properties = dict(line.split('=', 1) for line in subprocess.check_output(
    ['systemctl', '--user', 'show', unit, '-p', 'MemoryMax', '-p', 'CPUQuotaPerSecUSec',
     '-p', 'RuntimeMaxUSec'], text=True).splitlines())
assert properties['MemoryMax'] == '2147483648', properties
assert properties['CPUQuotaPerSecUSec'] == '1s', properties
assert properties['RuntimeMaxUSec'] == '15min', properties
cgroup = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
cgpath = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
limits = {'memory.max': (cgpath / 'memory.max').read_text().strip(),
          'cpu.max': (cgpath / 'cpu.max').read_text().strip()}
assert limits['memory.max'] == '2147483648', limits
quota, period = map(int, limits['cpu.max'].split())
assert quota == period, limits
if phase == 'history':
    history_scope = json.loads((ROOT / 'artifacts/history-scope.json').read_text())
    assert history_scope['commit_count'] > 0
    head = subprocess.check_output(['git', '-C', str(ROOT / 'pilot/history'),
                                    'rev-parse', 'HEAD'], text=True).strip()
    assert head == history_scope['head'] == '9134485371fdd6d1c92c16562aa04d188c3f2125'
wrapper = ROOT / 'pilot/run-with-identity.sh'
if not wrapper.exists():
    wrapper = ROOT / 'snapshot/docs/research/b7-0-sandbox/run.sh'
evidence = {'unit': unit, 'properties': properties, 'cgroup': cgroup, 'limits': limits,
            'command': command, 'phase': phase, 'wrapper': str(wrapper),
            'wrapper_sha256': hashlib.sha256(wrapper.read_bytes()).hexdigest()}
(out / 'scope.json').write_text(json.dumps(evidence, indent=2) + '\n')
mountout = (ROOT / 'pilot/out' / ('coverage-fixed' if phase == 'coverage-json-fixed' else 'coverage')
            if phase.startswith('coverage-json') else out)
snapshot = ROOT / 'pilot/history' if phase == 'history' else ROOT / 'snapshot'
env = {'PATH': '/usr/bin:/bin',
       'R': '/home/buggy1111/.local/share/agent-lease/runtime-9134485',
       'TD': str(ROOT / 'pilot/tooldeps'), 'SNAP': str(snapshot), 'OUT': str(mountout)}
start = time.monotonic()
with (out / 'stdout.log').open('wb') as stdout, (out / 'stderr.log').open('wb') as stderr:
    run = subprocess.run(['unshare', '--user', '--map-current-user', '--net',
                          '/bin/sh', str(wrapper), *command], env=env,
                         stdout=stdout, stderr=stderr)
result = {'phase': phase, 'returncode': run.returncode, 'seconds': time.monotonic() - start,
          'stdout_bytes': (out / 'stdout.log').stat().st_size,
          'stderr_bytes': (out / 'stderr.log').stat().st_size}
(out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
sys.exit(run.returncode)
