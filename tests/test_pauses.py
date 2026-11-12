"""Pausing a check whose owner knows it is down (2026-11-02: a server switched off
until another project is done)."""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient
from test_alerts import configure, down, setup, tg, up  # noqa: F401 (fixtures)
from test_auth import sign_up

from argus.alerts import prometheus_rules
from argus.app import create_app
from argus.probes import Result
from argus.runtime import Runtime
from argus.state import Store

OK, FAIL = Result(True, 0.1), Result(False, 0.2, "refused")


def test_a_pause_ends_when_the_check_is_back_up(tmp_path):
    s = Store(tmp_path / "a.db")
    for t in range(3):
        s.record("ollama", FAIL, t)
    s.pause("ollama", "off until the GPU project is done", "up", 10)
    s.record("ollama", FAIL, 11)
    assert s.pause_of("ollama")["reason"] == "off until the GPU project is done"
    s.record("ollama", OK, 12)
    assert s.pause_of("ollama") is None
    notes = [(t["from_status"], t["to_status"], t["error"]) for t in s.transitions("ollama")]
    assert notes[0] == ("paused", "up", "resumed on its own: back up")
    assert ("down", "paused", "off until the GPU project is done") in notes


def test_paused_while_up_it_waits_for_a_fall_and_a_recovery(tmp_path):
    # Paused ahead of planned work: the first success is not the end of it.
    s = Store(tmp_path / "a.db")
    s.record("nas", OK, 1)
    s.pause("nas", "disk swap", "up", 2)
    s.record("nas", OK, 3)
    assert s.pause_of("nas") is not None
    s.record("nas", FAIL, 4)
    s.record("nas", OK, 5)
    assert s.pause_of("nas") is None


def test_until_resumed_outlasts_a_recovery(tmp_path):
    s = Store(tmp_path / "a.db")
    for t in range(3):
        s.record("lab", FAIL, t)
    s.pause("lab", "", "resumed", 5)
    s.record("lab", OK, 6)
    assert s.pause_of("lab") is not None
    assert s.resume("lab", 7) and not s.resume("lab", 8)
    assert s.transitions("lab")[0]["error"] == "resumed"


def test_pausing_again_keeps_when_it_started(tmp_path):
    s = Store(tmp_path / "a.db")
    s.pause("x", "first", "up", 1)
    s.pause("x", "second", "resumed", 9)
    p = s.pause_of("x")
    assert (p["reason"], p["since"], p["until"]) == ("second", 1, "resumed")


def test_prune_forgets_a_removed_checks_pause(tmp_path):
    s = Store(tmp_path / "a.db")
    s.pause("gone", "", "up", 1)
    s.prune({"web"})
    assert s.pauses() == {}


async def test_a_paused_check_never_pages_and_pausing_is_not_a_recovery(setup, tg, clock):  # noqa: F811
    rt, settings, alerter = setup
    configure(settings)
    settings.set("alerts.rules", {"heartbeats": False})  # the hours below would trip it
    down(rt, "vault", clock)
    await alerter.evaluate()
    assert len(tg.sent) == 1 and tg.sent[0].startswith("🔴 vault is down")
    rt.store.pause("vault", "known", "up", clock.now)
    await alerter.evaluate()
    assert len(tg.sent) == 1  # no "Resolved"
    clock.now += 3 * 3600
    down(rt, "vault", clock)
    await alerter.evaluate()
    assert len(tg.sent) == 1 and alerter.problems() == {}
    up(rt, "vault", clock)  # back: the pause ends, nothing to say
    await alerter.evaluate()
    assert len(tg.sent) == 1 and rt.store.pause_of("vault") is None
    down(rt, "vault", clock)  # down again, no longer paused: pages
    await alerter.evaluate()
    assert len(tg.sent) == 2 and tg.sent[1].startswith("🔴 vault is down")


def test_prometheus_rule_skips_paused_checks():
    assert "argus_check_up == 0 unless on (check) argus_check_paused == 1" in prometheus_rules()


@pytest.fixture
def admin(config_file, tmp_path, prober, clock, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    rt = Runtime(config_file, tmp_path / "argus.db", prober=prober, clock=clock)
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c, {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}, rt


def test_pause_and_resume_through_the_api(admin, prober, clock):
    c, csrf, rt = admin
    prober.script["db"] = [FAIL] * 3
    loop = asyncio.new_event_loop()
    try:
        for _ in range(3):
            clock.now += 61
            loop.run_until_complete(rt._run(rt.config.checks[1]))
    finally:
        loop.close()
    assert c.put("/api/checks/db/pause", json={"reason": "x"}).status_code == 403  # no CSRF
    r = c.put("/api/checks/db/pause", headers=csrf,
              json={"reason": " switched off for now ", "until": "resumed"})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["status"] == "paused" and view["probe_status"] == "down"
    assert view["paused"]["reason"] == "switched off for now"
    assert view["paused"]["until"] == "resumed"
    assert view["transitions"][0]["to_status"] == "paused"
    checks = {x["name"]: x for x in c.get("/api/checks").json()}
    assert checks["db"]["status"] == "paused" and checks["web"]["paused"] is None
    rt.last_tick = clock.now
    metrics = c.get("/metrics").text
    assert 'argus_check_paused{check="db"' in metrics and "} 1.0" in metrics.split(
        'argus_check_paused{check="db"')[1].splitlines()[0]
    assert c.put("/api/checks/nope/pause", headers=csrf, json={}).status_code == 404
    assert c.put("/api/checks/db/pause", headers=csrf,
                 json={"until": "tomorrow"}).status_code == 422
    r = c.delete("/api/checks/db/pause", headers=csrf)
    assert r.json()["status"] == "down" and r.json()["paused"] is None
