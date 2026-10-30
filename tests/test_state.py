from argus.probes import Result
from argus.state import Store

OK, FAIL = Result(True, 0.1), Result(False, 0.2, "HTTP 500, expected 200")


def test_down_only_after_three_consecutive_failures(tmp_path):
    s = Store(tmp_path / "a.db")
    seq = [s.record("web", r, t).status for t, r in enumerate([OK, FAIL, FAIL, FAIL, FAIL, OK])]
    assert seq == ["up", "failing", "failing", "down", "down", "up"]
    st = s.get("web")
    assert (st.consecutive_failures, st.last_ok, st.last_change) == (0, 5, 5)


def test_a_success_resets_the_count(tmp_path):
    s = Store(tmp_path / "a.db")
    for t, r in enumerate([FAIL, FAIL, OK, FAIL, FAIL]):
        st = s.record("web", r, t)
    assert (st.status, st.consecutive_failures) == ("failing", 2)


def test_transitions_are_recorded_newest_first(tmp_path):
    s = Store(tmp_path / "a.db")
    for t, r in enumerate([OK, FAIL, FAIL, FAIL, OK]):
        s.record("web", r, t)
    got = [(x["from_status"], x["to_status"]) for x in s.transitions("web")]
    assert got == [("down", "up"), ("failing", "down"), ("up", "failing"), ("unknown", "up")]
    assert s.transitions("web")[1]["error"] == "HTTP 500, expected 200"


def test_state_survives_a_restart(tmp_path):
    s = Store(tmp_path / "a.db")
    s.record("web", FAIL, 1)
    s.record("web", FAIL, 2)
    s.beat("sync", 3, "10.0.0.1")
    s.close()
    s = Store(tmp_path / "a.db")
    assert s.record("web", FAIL, 4).status == "down"
    assert s.heartbeats()["sync"]["source"] == "10.0.0.1"


def test_prune_forgets_removed_names(tmp_path):
    s = Store(tmp_path / "a.db")
    s.record("old", FAIL, 1)
    s.record("web", OK, 1)
    s.beat("gone", 1, "x")
    s.prune({"web"})
    assert set(s.all()) == {"web"}
    assert s.heartbeats() == {} and s.transitions("old") == []
