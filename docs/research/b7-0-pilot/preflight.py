"""Harmless in-sandbox assertions; no project code or external connection."""
import errno
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import pwd
import signal
import socket
import sys

assert not Path('/home').exists()
assert not Path('/run/user/1000').exists()
assert len(Path('/proc/net/route').read_text().splitlines()) == 1
try:
    Path('/work/SHOULD_NOT_WRITE').write_text('probe')
    raise AssertionError('Snapshot writable')
except OSError as error:
    assert error.errno == errno.EROFS
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
with Path('/out/size-probe').open('wb') as stream:
    try:
        os.ftruncate(stream.fileno(), 52428801)
        raise AssertionError('File limit not enforced')
    except OSError as error:
        assert error.errno == errno.EFBIG
with socket.socket() as server, socket.socket() as client:
    server.settimeout(1)
    client.settimeout(1)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    client.connect(server.getsockname())
    connected, _ = server.accept()
    connected.close()
assert resource.getrlimit(resource.RLIMIT_FSIZE) == (52428800, 52428800)
assert resource.getrlimit(resource.RLIMIT_NPROC) == (256, 256)
assert pwd.getpwuid(os.getuid()).pw_dir == '/tmp/home'
saved_home = os.environ.pop('HOME')
assert Path.home() == Path('/tmp/home')
os.environ['HOME'] = saved_home
versions = {name: importlib.metadata.version(name) for name in ['coverage', 'lizard', 'Pygments', 'pathspec']}
assert versions == {'coverage': '7.17.2', 'lizard': '1.25.0', 'Pygments': '2.21.0', 'pathspec': '1.1.1'}, versions
output = {'status': 'pass', 'host_home_hidden': True, 'broker_hidden': True,
          'routes_empty': True, 'snapshot_readonly': True, 'loopback': True,
          'fsize': 52428800, 'nproc': 256, 'python': sys.version.split()[0], 'tools': versions}
Path('/out/preflight.json').write_text(json.dumps(output, indent=2) + '\n')
print(json.dumps(output))
