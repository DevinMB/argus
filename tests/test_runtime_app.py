import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from argus.app import create_app
from argus.probes import Result
from argus.runtime import Runtime


@pytest.fixture
def rt(config_file, tmp_path, prober, clock):
    return Runtime(config_file, tmp_path / "argus.db", prober=prober, clock=clock)


async def drain(rt):
    while rt._running:
        await asyncio.gather(*list(rt._running))


async def test_each_check_runs_once_per_interval(rt, prober, clock):
    rt.tick()  # schedules first runs within 10 s
    clock.now += 10
    rt.tick()
    await drain(rt)
    assert sorted(prober.calls) == ["db", "web"]
    clock.now += 29
    rt.tick()
    await drain(rt)
    assert len(prober.calls) == 2
    clock.now += 2  # db (30 s) is due, web (60 s) is not
    rt.tick()
    await drain(rt)
    assert sorted(prober.calls) == ["db", "db", "web"]


async def run_n(rt, clock, n):
    for _ in range(n):
        clock.now += 61
        rt.tick()
        await drain(rt)


async def test_down_after_three_failures_end_to_end(rt, prober, clock):
    prober.script["web"] = [Result(False, 0.1, "HTTP 502, expected 200")] * 3
    rt.tick()
    await run_n(rt, clock, 2)
    assert rt.store.get("web").status == "failing"
    await run_n(rt, clock, 1)
    assert rt.store.get("web").status == "down"


def bump(path, text):
    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


async def test_reload_applies_a_valid_config_and_prunes_state(rt, config_file, clock):
    rt.tick()
    await run_n(rt, clock, 1)
    bump(config_file, "checks:\n  - {name: web, kind: ping, target: h, site: B}\n")
    rt.tick()
    assert [c.name for c in rt.config.checks] == ["web"] and rt.reload_ok
    assert set(rt.store.all()) <= {"web"}


async def test_reload_keeps_running_config_when_new_one_is_invalid(rt, config_file):
    before = rt.config
    bump(config_file, "checks:\n  - {name: web, kind: http, target: nope, site: B}\n")
    rt.tick()
    assert rt.config is before and not rt.reload_ok


@pytest.fixture
def client(rt):
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c


def test_metrics_and_api(client, rt, prober, clock):
    assert "argus_check_up{" not in client.get("/metrics").text  # nothing ran yet
    prober.script["db"] = [Result(False, 0.1, "refused")] * 3
    loop = asyncio.new_event_loop()
    try:
        for _ in range(3):  # three scripted failures: db ends down, web up
            clock.now += 61
            for c in rt.config.checks:
                loop.run_until_complete(rt._run(c))
    finally:
        loop.close()
    rt.last_tick = clock.now
    text = client.get("/metrics").text
    assert ('argus_check_up{check="db",group="default",kind="tcp",severity="critical",'
            'site="A",target="127.0.0.1:1"} 0.0') in text
    assert 'argus_check_up{check="web"' in text and "argus_config_checks 2.0" in text
    assert 'argus_build_info{commit="unknown",config="' in text
    checks = {c["name"]: c for c in client.get("/api/checks").json()}
    assert checks["db"]["status"] == "down" and checks["web"]["status"] == "up"
    detail = client.get("/api/checks/db").json()
    assert detail["transitions"][0]["to_status"] == "down"
    assert client.get("/api/checks/nope").status_code == 404


def test_heartbeats(client, clock):
    assert client.get("/api/heartbeats").json()[0]["status"] == "never"
    assert client.post("/heartbeat/stack-sync").status_code == 204
    assert client.post("/heartbeat/unknown").status_code == 404
    hb = client.get("/api/heartbeats").json()[0]
    assert hb["status"] == "ok" and hb["source"] == "testclient"
    assert 'argus_heartbeat_last_seen_timestamp_seconds{group="default",heartbeat="stack-sync"' \
        in client.get("/metrics").text
    clock.now += 901
    assert client.get("/api/heartbeats").json()[0]["status"] == "late"


def test_a_heartbeat_can_say_which_version_runs(client, clock):
    assert client.post("/heartbeat/stack-sync?version=abc1234").status_code == 204
    first = clock.now
    clock.now += 600
    assert client.post("/heartbeat/stack-sync?version=abc1234").status_code == 204
    clock.now += 600
    assert client.post("/heartbeat/stack-sync").status_code == 204  # no version: keeps it
    hb = client.get("/api/heartbeats").json()[0]
    assert hb["version"] == "abc1234" and hb["version_at"] == first
    clock.now += 600
    client.post("/heartbeat/stack-sync?version=def5678")
    assert client.get("/api/heartbeats").json()[0]["version_at"] == clock.now
    assert client.post("/heartbeat/stack-sync?version=$(id)").status_code == 422


def test_healthz_reflects_the_scheduler(client, rt, clock):
    assert client.get("/healthz").status_code == 503
    rt.last_tick = clock.now
    assert client.get("/healthz").status_code == 200
    clock.now += 31
    assert client.get("/healthz").status_code == 503


def test_every_write_but_the_timers_and_sign_in_needs_the_admin(client):
    paths = {(r.path, m) for r in client.app.routes for m in getattr(r, "methods", ())}
    writes = {(p, m) for p, m in paths if m not in ("GET", "HEAD")}
    open_writes = {("/heartbeat/{name}", "POST"), ("/job/{name}", "POST"),
                   ("/api/setup", "POST"), ("/api/login", "POST")}
    assert open_writes <= writes
    for p, m in writes - open_writes:
        assert client.request(m, p, json={}).status_code == 401, (m, p)


def test_security_headers_on_every_response(client):
    for path in ("/healthz", "/api/info", "/metrics"):
        h = client.get(path).headers
        assert "default-src 'self'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"


def test_info_reports_server_time(client, clock):
    info = client.get("/api/info").json()
    assert info["now"] == clock.now and info["groups"] == {}
    assert info["sites"] == {"B": "B", "A": "A"} and info["links"] == []


def test_serves_the_ui_when_built(tmp_path, monkeypatch, rt):
    import argus.app as appmod
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<title>Argus</title>")
    monkeypatch.setattr(appmod, "UI_DIR", tmp_path / "ui")
    with TestClient(appmod.create_app(rt, start_scheduler=False)) as c:
        assert "<title>Argus</title>" in c.get("/").text
        assert c.get("/api/info").status_code == 200  # API still wins over the mount
        assert c.get("/openapi.json").status_code == 404
