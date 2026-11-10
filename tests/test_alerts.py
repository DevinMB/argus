import json
import logging
import textwrap

import httpx
import pytest
import yaml

from argus.alerts import TOKEN, WATCH_URL, Alerter, prometheus_rules, recent_chats
from argus.metrics import registry
from argus.probes import Result
from argus.runtime import Runtime
from argus.settings import Settings, load_key

BOT = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef"

CONFIG = """
sites: {home: Home}
checks:
  - {name: vault, kind: ping, target: h, severity: critical}
  - {name: blog, kind: ping, target: h, severity: critical}
  - {name: toy, kind: ping, target: h}
heartbeats:
  - {name: sync, max_age: 900, severity: critical}
jobs:
  - {name: guests, max_age: 3600, severity: critical, title: Every guest}
"""


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.status = 200
        self.pings: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "watch.example":
            self.pings.append(str(request.url))
            return httpx.Response(self.status)
        if self.status != 200:
            return httpx.Response(self.status, json={"ok": False, "description": "Unauthorized"})
        self.sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def setup(tmp_path, prober, clock, tg):
    path = tmp_path / "argus.yml"
    path.write_text(textwrap.dedent(CONFIG))
    rt = Runtime(path, tmp_path / "argus.db", prober=prober, clock=clock)
    settings = Settings(rt.store.db, load_key(tmp_path / "argus.key"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(tg))
    alerter = Alerter(rt, settings, client)
    return rt, settings, alerter


def configure(settings):
    settings.set_secret(TOKEN, BOT)
    settings.set("alerts.telegram.chat_id", "42")


def down(rt, name, clock, n=3):
    for _ in range(n):
        rt.store.record(name, Result(False, 0.1, "no reply"), clock.now)


def up(rt, name, clock):
    rt.store.record(name, Result(True, 0.01), clock.now)


async def test_a_critical_check_down_pages_once_then_repeats_then_resolves(setup, tg, clock):
    rt, settings, alerter = setup
    configure(settings)
    settings.set("alerts.repeat_minutes", 60)
    settings.set("alerts.rules", {"heartbeats": False})  # the hour below would trip it
    down(rt, "toy", clock)  # a warning: never pages
    down(rt, "vault", clock, n=2)  # failing, not down yet
    await alerter.evaluate()
    assert tg.sent == []
    down(rt, "vault", clock, n=1)
    await alerter.evaluate()
    assert len(tg.sent) == 1 and tg.sent[0].startswith("🔴 vault is down")
    assert "ping h: no reply" in tg.sent[0]
    clock.now += 59 * 60
    await alerter.evaluate()
    assert len(tg.sent) == 1
    clock.now += 61
    await alerter.evaluate()
    assert len(tg.sent) == 2 and tg.sent[1].startswith("🔁 Still, for 60m: vault is down")
    up(rt, "vault", clock)
    await alerter.evaluate()
    assert tg.sent[2].startswith("✅ Resolved: vault is down (for 60m")
    await alerter.evaluate()
    assert len(tg.sent) == 3


async def test_problems_that_start_together_are_one_message(setup, tg, clock):
    rt, settings, alerter = setup
    configure(settings)
    settings.set("alerts.link", "https://argus.lan:8080")
    down(rt, "vault", clock)
    down(rt, "blog", clock)
    await alerter.evaluate()
    assert len(tg.sent) == 1
    assert tg.sent[0].startswith("🔴 2 problems\n• vault is down")
    assert "• blog is down" in tg.sent[0]
    assert tg.sent[0].endswith("\nhttps://argus.lan:8080/")


async def test_jobs_and_quiet_heartbeats(setup, tg, clock):
    rt, settings, alerter = setup
    configure(settings)
    rt.store.record_run("guests", {"started": clock.now - 60, "finished": clock.now,
                                   "result": "failed", "summary": "couchdb rolled back",
                                   "log": ""}, clock.now, "test")
    await alerter.evaluate()
    assert tg.sent == ["🔴 Every guest failed\ncouchdb rolled back"]
    # A heartbeat never seen waits for its limit after Argus starts.
    clock.now += 901
    await alerter.evaluate()
    assert tg.sent[-1].startswith("🔴 sync has gone quiet\nNo beat yet")


async def test_a_failed_send_is_reported_without_the_token_and_retried(setup, tg, clock, caplog):
    rt, settings, alerter = setup
    configure(settings)
    caplog.set_level(logging.DEBUG)
    tg.status = 401
    down(rt, "vault", clock)
    await alerter.evaluate()
    err = alerter.view()["delivery"]["error"]
    assert "401" in err and "bot token" in err
    assert BOT not in caplog.text and BOT not in json.dumps(alerter.view())
    tg.status = 200
    clock.now += 30
    await alerter.evaluate()
    assert tg.sent == []  # waits a minute after a failure
    clock.now += 31
    await alerter.evaluate()
    assert len(tg.sent) == 1 and alerter.view()["delivery"].get("error") is None


async def test_handoff_to_prometheus_and_back_leaves_no_gap(setup, tg, clock):
    rt, settings, alerter = setup
    configure(settings)
    assert alerter.switch("prometheus") is not None  # no Prometheus test yet
    alerter.prometheus_test()
    assert alerter.prometheus_test_active()
    assert alerter.switch("prometheus") is None
    down(rt, "vault", clock)
    await alerter.evaluate()
    assert tg.sent == []  # Prometheus's job now
    assert alerter.switch("argus") is not None  # no Argus test yet
    assert await alerter.test() is None
    assert tg.sent == ["✅ Test from Argus: alerts reach you."]
    assert alerter.switch("argus") is None
    await alerter.evaluate()
    assert tg.sent[-1].startswith("🔴 vault is down")  # the open problem, sent once


async def test_test_without_telegram_says_what_is_missing(setup):
    _, _, alerter = setup
    assert "bot token" in await alerter.test()


async def test_the_watcher_is_pinged_only_while_the_scheduler_runs(setup, tg, clock):
    rt, settings, alerter = setup
    settings.set_secret(WATCH_URL, "https://watch.example/ping/secret-uuid")
    rt.last_tick = clock.now
    await alerter.ping_watcher()
    assert tg.pings == ["https://watch.example/ping/secret-uuid"]
    assert "ok_at" in alerter.view()["watch"]
    clock.now += 60  # no tick since: hung
    await alerter.ping_watcher()
    assert len(tg.pings) == 1
    rt.last_tick = clock.now
    tg.status = 500
    await alerter.ping_watcher()
    watch = alerter.view()["watch"]
    assert watch["error"] == "HTTP 500" and "secret-uuid" not in json.dumps(alerter.view())


def test_prometheus_rules_are_valid_and_include_the_test(setup):
    rt, _, alerter = setup
    rules = yaml.safe_load(prometheus_rules("https://argus.lan"))
    names = [r["alert"] for r in rules["groups"][0]["rules"]]
    assert {"ArgusDown", "ArgusCheckDown", "ArgusJobFailed", "ArgusTestAlert"} <= set(names)
    check = next(r for r in rules["groups"][0]["rules"] if r["alert"] == "ArgusCheckDown")
    assert "https://argus.lan/#/check/{{ $labels.check }}" in check["annotations"]["description"]
    reg = registry(rt, None, alerter.prometheus_test_active)
    assert reg.get_sample_value("argus_alert_test") == 0
    alerter.prometheus_test()
    assert reg.get_sample_value("argus_alert_test") == 1


async def test_recent_chats_lists_who_messaged_the_bot():
    updates = {"ok": True, "result": [
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Dev", "last_name": "B"}}},
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Dev", "last_name": "B"}}},
        {"channel_post": {"chat": {"id": -100123, "type": "channel", "title": "Ops"}}},
        {"edited_message": {}},
    ]}
    ok = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=updates)))
    assert await recent_chats(BOT, ok) == [
        {"id": "42", "name": "Dev B", "kind": "private"},
        {"id": "-100123", "name": "Ops", "kind": "channel"}]
    bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(Exception, match="check the bot token") as e:
        await recent_chats(BOT, bad)
    assert BOT not in str(e.value)
