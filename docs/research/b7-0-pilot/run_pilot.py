"""Enter an approved resource scope. Commands run only through the reviewed wrapper."""
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parent
phase, *command = sys.argv[1:]
assert phase.replace('-', '').replace('_', '').isalnum()
unit = f'agent-lease-b7-{phase}-{int(time.time())}.scope'
env = {'HOME': '/home/buggy1111', 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
       'XDG_RUNTIME_DIR': '/run/user/1000', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'}
run = subprocess.run(['systemd-run', '--user', '--scope', '--unit=' + unit,
                      '--property=MemoryMax=2G', '--property=CPUQuota=100%',
                      '--property=RuntimeMaxSec=15min', '/usr/bin/python3',
                      str(root / 'pilot_scope.py'), unit, phase, *command], env=env)
sys.exit(run.returncode)
