"""Bounded numeric artifacts and report-only thresholds."""
import ast
import json
import os

import pytest

from agent_lease_mcp._quality_runs import CommandObservation, MetricObservation, _command_result
from agent_lease_mcp.quality import Status
from agent_lease_mcp.quality_collect import MAX_REPORT, _owned, line_metric, read_report


@pytest.mark.parametrize("covered,total,expected", [(80,100,Status.PASS), (79,100,Status.REVIEW), (0,5,Status.REVIEW)])
def test_line_threshold_is_report_only(covered, total, expected):
    metric = MetricObservation("coverage.py", covered, total)
    observation = CommandObservation("coverage", 0, False, 1, 0, "0" * 64, metrics=metric)
    assert _command_result(observation)[0] == expected


@pytest.mark.parametrize("values", [{"covered": True}, {"total": 0}, {"covered": 101},
    {"format": "invented"}, {"crap_review": 1}, {"crap_known": 1},
    {"crap_max_milli": 0}, {"crap_known": 1, "crap_max_milli": True}])
def test_metric_counts_fail_closed(values):
    with pytest.raises(ValueError):
        MetricObservation(**({"format":"coverage.py", "covered":80, "total":100} | values))


def test_only_coverage_can_carry_metrics():
    with pytest.raises(ValueError):
        CommandObservation("tests", 0, False, 1, 0, "0"*64,
                           metrics=MetricObservation("istanbul", 1, 1))


@pytest.mark.parametrize("kind,report", [
    ("coverage.py", {"totals":{"covered_lines":8,"num_statements":10}}),
    ("istanbul", {"total":{"lines":{"covered":8,"total":10}}})])
def test_real_report_schemas(kind, report):
    assert line_metric(report, kind) == {"format":kind,"covered":8,"total":10}


@pytest.mark.parametrize("raw", [b'{}', b'{"a":1,"a":2}', b'[]', b'bad', b'['*2000])
def test_bounded_json_artifact(tmp_path, raw):
    (tmp_path / "report.json").write_bytes(raw)
    assert read_report(tmp_path, ("report.json",)) == ({} if raw == b'{}' else None)


def test_report_rejects_links_fifo_and_large_file(tmp_path):
    target = tmp_path / "target"
    target.write_text(json.dumps({"value":1}))
    link = tmp_path / "link"
    link.symlink_to(target)
    assert read_report(tmp_path, ("link",)) is None
    folder = tmp_path / "folder"
    folder.symlink_to(tmp_path, target_is_directory=True)
    assert read_report(tmp_path, ("folder", "target")) is None
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert read_report(tmp_path, ("fifo",)) is None
    with target.open("wb") as stream:
        stream.truncate(MAX_REPORT+1)
    assert read_report(tmp_path, ("target",)) is None


def test_crap_line_ownership_excludes_nested_decorators():
    module = ast.parse('@outer\ndef f():\n @inner\n def g():\n  return 1\n return 2\n')
    assert _owned(module.body[0], set(range(1,7))) == {1,2,6}
