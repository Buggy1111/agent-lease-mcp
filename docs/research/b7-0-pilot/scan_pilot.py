"""Run pinned scanners offline. Never print finding values or raw scanner output."""
import json
from pathlib import Path
import random
import string
import subprocess
import sys
import time

mode = sys.argv[1]
assert mode in {'synthetic', 'history'}
root = Path('/out')
secrets = []
positives = set()
negatives = set()
if mode == 'synthetic':
    corpus = root / 'corpus'
    corpus.mkdir()
    rng = random.Random(9134485)
    def token(length, alphabet=string.ascii_letters + string.digits):
        return ''.join(rng.choice(alphabet) for _ in range(length))
    values = ['ghp_' + token(40), 'sk-' + token(48), 'AKIA' + token(16, string.ascii_uppercase + string.digits),
              'xoxb-' + token(12, string.digits) + '-' + token(12, string.digits) + '-' + token(24),
              'AIza' + token(35, string.ascii_letters + string.digits + '_-')]
    names = ['github_token', 'api_key', 'aws_access_key_id', 'slack_token', 'google_api_key']
    for index, (name, value) in enumerate(zip(names, values)):
        filename = f'positive-{index}.txt'
        (corpus / filename).write_text(name + '="' + value + '"\n')
        positives.add(filename)
        secrets.append(value)
    for index, text in enumerate(['token = uuid.uuid4().hex',
                                  'password = get_password_from_vault(name2)',
                                  'error_token = "Invalid token for user 42"',
                                  'api_key = os.environ.get("API_KEY")',
                                  'PASSWORD="replace-me"']):
        filename = f'negative-{index}.txt'
        (corpus / filename).write_text(text + '\n')
        negatives.add(filename)
    (corpus / '.gitleaks.toml').write_text('THIS IS AN INTENTIONALLY INVALID TARGET CONFIG')
    (corpus / '.betterleaks.toml').write_text('THIS IS AN INTENTIONALLY INVALID TARGET CONFIG')
    target = str(corpus)
else:
    target = '/work'

results = []
for scanner in ['gitleaks', 'betterleaks']:
    report = root / (scanner + '-redacted.json')
    command = ['/tools/testdeps/bin/' + scanner, 'dir' if mode == 'synthetic' else 'git', target,
               '--config=/tools/testdeps/scanner.toml', '--gitleaks-ignore-path=/tools/testdeps/empty-ignore',
               '--ignore-gitleaks-allow', '--redact=100', '--no-banner', '--no-color',
               '--report-format=json', '--report-path=' + str(report), '--log-level=info',
               '--max-archive-depth=0', '--max-decode-depth=0', '--timeout=600']
    if scanner == 'betterleaks':
        command.append('--validation=false')
    if mode == 'history':
        command.append('--log-opts=--all')
    start = time.monotonic()
    completed = subprocess.run(command, capture_output=True)
    duration = time.monotonic() - start
    (root / (scanner + '-stdout.log')).write_bytes(completed.stdout)
    (root / (scanner + '-stderr.log')).write_bytes(completed.stderr)
    assert completed.returncode in (0, 1), f'{scanner}: command error; raw output intentionally hidden'
    assert report.exists(), f'{scanner}: no report'
    raw = report.read_bytes()
    outputs = [completed.stdout, completed.stderr, raw]
    if mode == 'synthetic':
        for value in secrets:
            for fragment in [value, value[len(value)//2:len(value)//2+12]]:
                assert not any(fragment.encode() in output for output in outputs), scanner + ': redaction failed'
    findings = json.loads(raw)
    assert isinstance(findings, list)
    for finding in findings:
        assert finding.get('Secret') in ('', 'REDACTED', '[REDACTED]', None), scanner + ': unredacted Secret field'
    sanitized = [{'rule': f.get('RuleID'), 'file': f.get('File'), 'line': f.get('StartLine'),
                  'commit': f.get('Commit')} for f in findings]
    (root / (scanner + '-findings-sanitized.json')).write_text(json.dumps(sanitized, indent=2) + '\n')
    detected = {Path(f.get('File', '')).name for f in findings}
    results.append({'scanner': scanner, 'mode': mode, 'seconds': duration,
                    'returncode': completed.returncode, 'findings': len(findings),
                    'true_positive_files': sorted(detected & positives),
                    'false_negative_files': sorted(positives - detected),
                    'false_positive_files': sorted(detected & negatives),
                    'redaction_checked': True, 'target_config_ignored': mode == 'synthetic',
                    'command': command})
    (root / 'scan-results.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results))
