"""Trusted namespace init: run a command and export numeric metrics only.

Executed with -I -S -B from a read-only mount. The project never inherits the
metadata descriptor. PR_SET_DUMPABLE=0 and dropped capabilities prevent it from
reopening that descriptor through /proc/1/fd. No raw artifact leaves the box.
"""
from __future__ import annotations

import ast
import ctypes
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

MAX_REPORT = 4 * 1024**2


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metric key")
        result[key] = value
    return result


def read_report(root: Path, parts: tuple[str, ...]) -> dict | None:
    """Never follow an artifact symlink, directory link, FIFO or device."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_REPORT:
                return None
            raw = bytearray()
            while len(raw) <= MAX_REPORT:
                chunk = os.read(file_fd, min(65536, MAX_REPORT + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_REPORT:
                return None
            result = json.loads(raw, object_pairs_hook=_object)
            return result if isinstance(result, dict) else None
        finally:
            os.close(file_fd)
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)


def _count(value):
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ValueError("invalid metric count")
    return value


def line_metric(report: dict, kind: str) -> dict:
    values = report["totals"] if kind == "coverage.py" else report["total"]["lines"]
    covered = _count(values["covered_lines"] if kind == "coverage.py" else values["covered"])
    total = _count(values["num_statements"] if kind == "coverage.py" else values["total"])
    if total == 0 or covered > total:
        raise ValueError("coverage must have attributable executable lines")
    return {"format": kind, "covered": covered, "total": total}


def _owned(node, executable):
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    owned = {line for line in executable if start <= line <= node.end_lineno}
    for child in ast.walk(node):
        if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            begin = min([child.lineno] + [d.lineno for d in child.decorator_list])
            owned.difference_update(range(begin, child.end_lineno + 1))
    return owned


def crap_metric(report: dict, source: Path) -> dict:
    """Lizard complexity + coverage.py line ownership, matching the B7.0 pilot.

    Analyze immutable source, not files a check may have rewritten. Python only;
    no function names, source paths or source text are exported.
    """
    sys.path.insert(0, "/tools/site")  # operator-pinned read-only tools, never /work
    try:
        import lizard
    except ImportError:
        return {}
    measured = report.get("files", {})
    if not isinstance(measured, dict):
        return {}
    files = {}
    for name, value in measured.items():
        relative = Path(name)
        if relative.is_absolute():
            try:
                relative = relative.relative_to("/work")
            except ValueError:
                continue
        if ".." not in relative.parts:
            files[relative.as_posix()] = value
    known = unknown = high = maximum = 0
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source)
        if any(p in {"tests", "test", "__tests__"} for p in relative.parts) or path.name.startswith("test_"):
            continue
        if path.stat().st_size > 1024**2:
            unknown += 1
            continue
        try:
            nodes = {n.lineno: n for n in ast.walk(ast.parse(path.read_text()))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            data = files.get(relative.as_posix())
            if data is not None:
                executed = {_count(line) for line in data["executed_lines"]}
                missing = {_count(line) for line in data["missing_lines"]}
                excluded = {_count(line) for line in data.get("excluded_lines", [])}
                executed -= excluded
                missing -= excluded
                if executed & missing:
                    raise ValueError("overlapping coverage lines")
            else:
                executed = missing = set()
            functions = lizard.analyze_file(str(path)).function_list
            for function in functions:
                if known + unknown >= 10000:
                    return {}  # unsupported size is not a passing metric
                node = nodes.get(function.start_line)
                lines = _owned(node, executed | missing) if node is not None else set()
                complexity = function.cyclomatic_complexity
                if not lines or type(complexity) is not int or not 1 <= complexity <= 10000:
                    unknown += 1
                    continue
                total, uncovered = len(lines), len(lines - executed)
                denominator = total**3
                numerator = complexity**2 * uncovered**3 + complexity * denominator
                maximum = max(maximum, (numerator * 1000 + denominator // 2) // denominator)
                high += numerator > 30 * denominator
                known += 1
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            unknown += 1
    return {"crap_known": known, "crap_unknown": unknown, "crap_review": high,
            "crap_max_milli": maximum if known else None}


def collect(work: Path, source: Path) -> dict:
    report = read_report(work, ("coverage.json",))
    try:
        if report is not None:
            metric = line_metric(report, "coverage.py")
            try:
                metric.update(crap_metric(report, source))
            except Exception:  # noqa: BLE001 -- optional analysis failure is N/A, never PASS
                metric.update(crap_known=0, crap_unknown=1, crap_review=0, crap_max_milli=None)
            return metric
        report = read_report(work, ("coverage", "coverage-summary.json"))
        return line_metric(report, "istanbul") if report is not None else {}
    except (ValueError, TypeError, KeyError, RecursionError):
        return {}


def main() -> int:
    try:
        fd, check, *command = sys.argv[1:]
        fd = int(fd)
        if not command or check not in {"coverage", "none"}:
            return 2
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(4, 0) != 0:  # PR_SET_DUMPABLE=0; child cannot reopen metadata fd
            return 2
        if check == "coverage":
            # Discard committed/stale reports before any project process starts.
            for parts in (("coverage.json",), ("coverage", "coverage-summary.json")):
                path = Path("/work").joinpath(*parts)
                if path.exists() and path.is_file():
                    path.unlink()
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
        # Namespace PID 1 must reap adopted orphans while the project is still
        # running. Otherwise zombies keep process groups apparently alive and
        # break projects that verify cleanup of their own subprocess trees.
        while True:
            pid, status = os.waitpid(-1, 0)
            if pid == child.pid:
                code = child.returncode = os.waitstatus_to_exitcode(status)
                break
        metric = {}
        if code == 0 and check == "coverage":
            try:
                metric = collect(Path("/work"), Path("/snapshot"))
            except Exception:  # noqa: BLE001 -- bad metrics cannot turn command success into a fake metric
                metric = {}
        payload = json.dumps(metric, separators=(",", ":")).encode()
        if len(payload) > 4096 or os.write(fd, payload) != len(payload):
            return 2
        return code if code >= 0 else 128 - code
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
