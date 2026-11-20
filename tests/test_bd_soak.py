"""Small checks of the soak's concurrency, oracle and synthetic scenarios."""
import pytest
from soak_bd import Pace, Soak


def test_pacing_reserves_at_most_two_calls_per_second(monkeypatch):
    import soak_bd

    clock = [0.0]
    monkeypatch.setattr(soak_bd.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(soak_bd.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    pace = Pace()
    granted = []
    for count in (2, 1, 1, 2, 1):
        pace.reserve(count)
        granted.extend([clock[0]] * count)
    assert all(sum(t - 1 < other <= t for other in granted) <= 2 for t in granted)


def test_worker_error_is_not_lost():
    pace = Pace()

    def fail():
        raise RuntimeError("synthetic worker failure")

    with pytest.raises(RuntimeError, match="synthetic worker failure"):
        pace.race(fail, lambda: True)


def test_mixed_scenarios_and_reopen_keep_invariants(tmp_path):
    soak = Soak(tmp_path)
    soak.pace.reserve = lambda count=1: None
    soak.capacity()
    for number in range(5):
        for scenario in (soak.review, soak.fix, soak.research, soak.quality):
            scenario(number)
        soak.reopen()
        soak.check()
    assert all(soak.counts[name] == 5 for name in ("reviews", "fixes", "research", "quality"))
    assert all(soak.counts[name] for name in (
        "research_cancel", "research_budget", "research_unknown", "research_cutoff"))


def test_oracle_rejects_orphan_quality_result(tmp_path):
    soak = Soak(tmp_path)
    request = soak.stores[0].quality_request("codex", str(soak.repo))
    with soak.stores[0]._connect() as con:
        con.execute("UPDATE quality_requests SET state='completed',run_id=999 WHERE id=?",
                    (request["id"],))
    with pytest.raises(AssertionError):
        soak.check()
