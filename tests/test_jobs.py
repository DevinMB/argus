"""Jobs (decision 0017): systemd runs them on ops-runtime, they report here."""

import textwrap

import pytest
from fastapi.testclient import TestClient

from argus import config as cfg
from argus.app import LOG_TAIL, MAX_REPORT, create_app
from argus.metrics import registry
from argus.runtime import Runtime
from argus.state import KEEP_RUNS

CONFIG = """
checks:
  - {name: web, kind: ping, target: h, site: B}
jobs:
  - {name: guests, max_age: 691200, schedule: "Mondays 03:00", site: B, severity: critical}
"""


@pytest.fixture
def rt(tmp_path, prober, clock):
    p = tmp_path / "argus.yml"
    p.write_text(textwrap.dedent(CONFIG))
    return Runtime(p, tmp_path / "argus.db", prober=prober, clock=clock)


@pytest.fixture
def client(rt):
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c


def run(result="ok", started=999_000.0, finished=999_500.0, **kw):
    return {"result": result, "started": started, "finished": finished, **kw}


def stored(*a, **kw):
    """A run as Runtime.report receives it: JobRun's defaults filled in."""
    return {"summary": "", "log": "", "items": [], **run(*a, **kw)}


def view(client):
    return client.get("/api/jobs").json()[0]


def test_a_job_is_never_until_it_reports(client):
    v = view(client)
    assert v["status"] == "never" and v["last"] is None and v["schedule"] == "Mondays 03:00"


def test_ok_then_failed_then_ok(client):
    assert client.post("/job/guests", json=run(summary="16 guests patched")).status_code == 204
    assert view(client)["status"] == "ok"
    failed = run("failed", 999_600, 999_700, summary="herdr-lab rolled back")
    client.post("/job/guests", json=failed)
    v = view(client)
    assert v["status"] == "failed" and v["last"]["summary"] == "herdr-lab rolled back"
    assert v["last_success"] == 999_500.0
    client.post("/job/guests", json=run(started=999_800, finished=999_900))
    assert view(client)["status"] == "ok"


def test_late_after_max_age(client, clock):
    client.post("/job/guests", json=run())
    clock.now = 999_500 + 691_200 + 1
    assert view(client)["status"] == "late"


def test_detail_has_runs_with_logs_newest_first(client):
    client.post("/job/guests", json=run(log="first"))
    client.post("/job/guests", json=run(started=999_600, finished=999_700, log="second"))
    d = client.get("/api/jobs/guests").json()
    assert [r["log"] for r in d["runs"]] == ["second", "first"]
    assert "log" not in d["last"]
    assert client.get("/api/jobs/nope").status_code == 404


def test_only_configured_jobs_and_valid_reports(client):
    assert client.post("/job/nope", json=run()).status_code == 404
    assert client.post("/job/guests", json=run("maybe")).status_code == 422
    assert client.post("/job/guests", json=run(started=2, finished=1)).status_code == 422
    assert client.post("/job/guests", json={**run(), "extra": 1}).status_code == 422
    assert client.post("/job/guests", content=b"x" * (MAX_REPORT + 1)).status_code == 413
    assert view(client)["status"] == "never"


def test_a_run_lists_what_it_covered(client):
    items = [{"name": "couchdb", "result": "ok", "detail": "41 packages"},
             {"name": "news-lab", "result": "skipped", "detail": "excluded"}]
    assert client.post("/job/guests", json=run(items=items)).status_code == 204
    assert view(client)["last"]["items"] == items
    assert client.get("/api/jobs/guests").json()["runs"][0]["items"] == items
    client.post("/job/guests", json=run())
    assert view(client)["last"]["items"] == []
    bad = [{"name": "x", "result": "sort of"}]
    assert client.post("/job/guests", json=run(items=bad)).status_code == 422
    assert client.post("/job/guests", json=run(items=[items[0]] * 201)).status_code == 422


def test_long_logs_keep_their_tail(client):
    client.post("/job/guests", json=run(log="a" * 1000 + "b" * LOG_TAIL))
    assert client.get("/api/jobs/guests").json()["runs"][0]["log"] == "b" * LOG_TAIL


def test_keeps_the_newest_runs_only(rt):
    for i in range(KEEP_RUNS + 5):
        rt.report("guests", stored(started=i + 1, finished=i + 2, summary=str(i)), "t")
    runs = rt.store.runs("guests")
    assert len(runs) == KEEP_RUNS and runs[0]["summary"] == str(KEEP_RUNS + 4)


def test_metrics(rt):
    def sample(name):
        for fam in registry(rt).collect():
            for s in fam.samples:
                if s.name == name:
                    return s
        return None

    assert sample("argus_job_last_result") is None
    assert sample("argus_job_max_age_seconds").value == 691200
    rt.report("guests", stored(), "t")
    rt.report("guests", stored("failed", 999_600, 999_700), "t")
    assert sample("argus_job_last_result").value == 0
    assert sample("argus_job_last_result").labels == {
        "job_name": "guests", "group": "default", "site": "B", "severity": "critical"}
    assert sample("argus_job_last_run_timestamp_seconds").value == 999_700
    assert sample("argus_job_last_success_timestamp_seconds").value == 999_500


def test_job_names_share_the_namespace():
    with pytest.raises(ValueError, match="duplicate names: web"):
        cfg.Config.model_validate({
            "checks": [{"name": "web", "kind": "ping", "target": "h", "site": "B"}],
            "jobs": [{"name": "web", "max_age": 60, "site": "B"}]})


def test_a_removed_job_is_forgotten(rt):
    rt.report("guests", stored(), "t")
    rt.store.prune({"web"})
    assert rt.store.last_runs() == {}
