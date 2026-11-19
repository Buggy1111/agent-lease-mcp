"""Read synthetic fixtures only; print counts, never values."""
import json
from pathlib import Path
from agent_lease_mcp.secret_scan import scan_diff

results = []
for path in sorted(Path('/tools/testdeps/inputs/corpus').glob('*.txt')):
    lines = path.read_text().splitlines()
    body = '\n'.join('+' + line for line in lines)
    diff = f'diff --git a/{path.name} b/{path.name}\n--- a/{path.name}\n+++ b/{path.name}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n'
    found = scan_diff(diff)
    results.append({'file': path.name, 'detected': bool(found), 'expected': path.name.startswith('positive-')})
summary = {'scanner': 'B6 assigned diff scanner at 9134485',
           'true_positive_files': sum(r['detected'] and r['expected'] for r in results),
           'false_negative_files': sum(not r['detected'] and r['expected'] for r in results),
           'false_positive_files': sum(r['detected'] and not r['expected'] for r in results),
           'results': results}
Path('/out/comparison.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps({key: value for key, value in summary.items() if key != 'results'}))
