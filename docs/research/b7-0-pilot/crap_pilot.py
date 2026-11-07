"""Pilot-only CRAP report. AST ownership excludes nested function/decorator ranges."""
import ast
import csv
import json
from pathlib import Path

def score(complexity, coverage):
    return None if coverage is None else complexity ** 2 * (1 - coverage) ** 3 + complexity

def begin(node):
    return min([node.lineno] + [d.lineno for d in node.decorator_list])

def owned_lines(node, executable):
    lines = {line for line in executable if begin(node) <= line <= node.end_lineno}
    for child in ast.walk(node):
        if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.difference_update(range(begin(child), child.end_lineno + 1))
    return lines

assert score(3, 1.0) == 3
assert score(3, 0.0) == 12
assert score(3, None) is None
assert score(3, 0.5) == 4.125
fixture = ast.parse('@decorator\nasync def outer():\n    x = 1\n    @decorator\n    def inner():\n        return 2\n    return x\n')
outer = fixture.body[0]
inner = next(n for n in ast.walk(outer) if isinstance(n, ast.FunctionDef))
assert owned_lines(outer, set(range(1, 8))) == {1, 2, 3, 7}
assert owned_lines(inner, set(range(1, 8))) == {4, 5, 6}
assert not (owned_lines(outer, set(range(1, 8))) & owned_lines(inner, set(range(1, 8))))
checks = {'100_percent_comp3': 3, '0_percent_comp3': 12, 'missing_coverage': None,
          '50_percent_comp3': 4.125, 'async_decorators_and_nested_ownership': 'pass'}
Path('/out/manual-checks.json').write_text(json.dumps(checks, indent=2) + '\n')

data = json.loads(Path('/tools/testdeps/inputs/coverage.json').read_text())
files = {name.removeprefix('/work/'): value for name, value in data['files'].items()}
cache = {}
reports = []
with Path('/tools/testdeps/inputs/lizard.csv').open() as stream:
    for row in csv.reader(stream):
        relative = row[6].removeprefix('/work/')
        if not relative.startswith('src/'):
            continue
        if relative not in cache:
            tree = ast.parse(Path('/work', relative).read_text())
            cache[relative] = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        start, end, complexity = int(row[9]), int(row[10]), int(row[1])
        candidates = [n for n in cache[relative] if n.lineno == start]
        node = candidates[0] if len(candidates) == 1 else None
        measured = files.get(relative)
        coverage = None
        executed_count = total = 0
        reason = None
        if node is None:
            reason = 'Function boundary not mapped to AST'
        elif measured is None:
            reason = 'File has no coverage data'
        else:
            executed = set(measured['executed_lines'])
            executable = executed | set(measured['missing_lines'])
            owned = owned_lines(node, executable)
            total = len(owned)
            executed_count = len(owned & executed)
            if total:
                coverage = executed_count / total
            else:
                reason = 'No attributable executable statements'
        value = score(complexity, coverage)
        reports.append({'file': relative, 'function': row[7], 'line': start,
                        'end_line': end, 'complexity': complexity,
                        'executed_statements': executed_count, 'executable_statements': total,
                        'coverage_fraction': coverage, 'crap': value, 'reason': reason,
                        'status': 'N/A' if value is None else 'REVIEW' if value > 30 else 'PASS'})

reports.sort(key=lambda r: (r['crap'] is None, -(r['crap'] or 0), r['file'], r['line']))
summary = {'commit': '9134485371fdd6d1c92c16562aa04d188c3f2125',
           'mode': 'report-only', 'threshold': 30,
           'functions': len(reports), 'review': sum(r['status'] == 'REVIEW' for r in reports),
           'na': sum(r['status'] == 'N/A' for r in reports),
           'ownership': 'AST function and decorator ranges, excluding nested functions; executable lines only',
           'coverage_totals': data['totals'], 'results': reports}
Path('/out/crap.json').write_text(json.dumps(summary, indent=2) + '\n')
with Path('/out/crap.csv').open('w') as stream:
    writer = csv.DictWriter(stream, fieldnames=list(reports[0]), lineterminator='\n')
    writer.writeheader()
    writer.writerows(reports)
print(json.dumps({key: value for key, value in summary.items() if key != 'results'}))
