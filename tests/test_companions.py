import base64
import json
import logging
import secrets
import textwrap

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus.alerts import Alerter
from argus.app import create_app
from argus.companions import CODE_TTL, DOMAIN, QUIET_AFTER, name_from
from argus.metrics import registry
from argus.runtime import Runtime
from argus.settings import Settings, load_key

CONFIG = """
checks:
  - {name: nas-web, kind: http, target: "http://127.0.0.1:5000/", companion: nas,
     severity: critical, interval: 30}
  - {name: outside, kind: tcp, target: "127.0.0.1:1"}
"""
PIN = "q83vEjRWeJq83vEjRWeJq83vEjRWeJq83vEjRWeJq8w="


@pytest.fixture
def rt(tmp_path, prober, clock):
    path = tmp_path / "argus.yml"
    path.write_text(textwrap.dedent(CONFIG))
    return Runtime(path, tmp_path / "argus.db", prober=prober, clock=clock)


@pytest.fixture
def client(rt, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    with TestClient(create_app(rt, start_scheduler=False, tls_pin=PIN)) as c:
        yield c


@pytest.fixture
def csrf(client, caplog):
    return sign_up(client, caplog)["csrf"]


def new_code(client, csrf, **body):
    r = client.post("/api/companions/codes", headers={"X-CSRF-Token": csrf},
                    json={"url": "https://argus.lan:8443", **body})
    assert r.status_code == 200, r.text
    return r.json()


def enroll(client, code, hostname="nas.lan"):
    return client.post("/companion/enroll", json={"code": code, "hostname": hostname})


def report(client, token, **fields):
    body = {"nonce": secrets.token_urlsafe(16), "hostname": "nas", **fields}
    return client.post("/companion/report", json=body,
                       headers={"Authorization": f"Bearer {token}"}), body["nonce"]


def verify(public_key, cid, nonce, r):
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
    msg = DOMAIN + cid.encode() + b"\n" + nonce.encode() + b"\n" + r.content
    key.verify(base64.b64decode(r.headers["x-argus-signature"]), msg)


def test_the_command_pins_argus_and_quotes_what_it_carries(client, csrf):
    out = new_code(client, csrf, read_only=True)
    assert out["command"].startswith(f"curl -fsS -k --pinnedpubkey sha256//{PIN} "
                                     "https://argus.lan:8443/companion/install.sh | sudo sh -s --")
    assert f"--pin {PIN}" in out["command"] and out["command"].endswith(" --read-only")
    assert out["code"] in out["command"]
    # Plain http: no pin, nothing to pin.
    plain = new_code(client, csrf, url="http://argus.lan:8080")["command"]
    assert "pinnedpubkey" not in plain and "--pin" not in plain
    odd = new_code(client, csrf, url="https://argus.lan/;reboot")["command"]
    assert "'https://argus.lan/;reboot/companion/install.sh'" in odd


def test_adding_a_host_needs_the_admin(client):
    r = client.post("/api/companions/codes", json={"url": "https://argus.lan"})
    assert r.status_code == 401


def test_a_code_enrolls_once_then_reports_are_signed(client, csrf, rt, clock):
    code = new_code(client, csrf)["code"]
    r = enroll(client, code)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["name"] == "nas" and me["token"].startswith("argc_")
    assert enroll(client, code).status_code == 403  # single use
    # Argus keeps only a hash of the token.
    row = rt.store.db.execute("SELECT * FROM companions").fetchone()
    assert me["token"] not in json.dumps(dict(row))

    r, nonce = report(client, me["token"], os="Debian GNU/Linux 13 (trixie)",
                      disks=[{"mount": "/", "total": 100, "used": 40}],
                      containers=[{"name": "web", "state": "running", "health": "healthy"}],
                      checks=[{"name": "nas-web", "ok": False, "duration": 0.2,
                               "error": "HTTP 502"},
                              {"name": "outside", "ok": False, "duration": 0.1}])
    assert r.status_code == 200, r.text
    verify(me["public_key"], me["id"], nonce, r)
    with pytest.raises(InvalidSignature):  # bound to this host and this nonce
        verify(me["public_key"], me["id"], "another-nonce-value", r)
    reply = r.json()
    assert reply["read_only"] is False and reply["report_every"] == 60
    assert reply["checks"] == [{"name": "nas-web", "kind": "http",
                                "target": "http://127.0.0.1:5000/", "interval": 30,
                                "timeout": 5.0, "expect_status": [200], "verify_tls": True}]
    # Only the check assigned to this companion took the result.
    assert rt.store.get("nas-web").consecutive_failures == 1
    assert rt.store.get("outside").last_run is None

    view = client.get("/api/companions").json()
    (nas,) = view["companions"]
    assert nas["status"] == "ok" and nas["report"]["os"].startswith("Debian")
    assert nas["report"]["containers"][0]["health"] == "healthy"
    assert nas["checks"][0]["name"] == "nas-web" and view["unknown"] == []


def test_codes_expire_and_names_stay_unique(client, csrf, clock):
    code = new_code(client, csrf)["code"]
    clock.now += CODE_TTL + 1
    assert "expired" in enroll(client, code).json()["detail"]
    a = enroll(client, new_code(client, csrf)["code"]).json()
    b = enroll(client, new_code(client, csrf)["code"]).json()
    c = enroll(client, new_code(client, csrf, name="vault")["code"]).json()
    assert (a["name"], b["name"], c["name"]) == ("nas", "nas-2", "vault")


def test_removing_a_host_revokes_its_token(client, csrf):
    me = enroll(client, new_code(client, csrf)["code"]).json()
    r = client.delete(f"/api/companions/{me['id']}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert report(client, me["token"])[0].status_code == 401
    assert report(client, "argc_guess")[0].status_code == 401


def test_a_read_only_install_is_told_so(client, csrf):
    me = enroll(client, new_code(client, csrf, read_only=True)["code"]).json()
    assert report(client, me["token"])[0].json()["read_only"] is True


def test_quiet_hosts_and_full_disks_page(client, csrf, rt, clock, tmp_path):
    me = enroll(client, new_code(client, csrf)["code"]).json()
    report(client, me["token"], disks=[{"mount": "/", "total": 1000, "used": 960},
                                       {"mount": "/srv", "total": 1000, "used": 500}])
    settings = Settings(rt.store.db, load_key(tmp_path / "argus.key"))
    alerter = Alerter(rt, settings, httpx.AsyncClient())
    assert {k: p.title for k, p in alerter.problems().items()} == {
        "disk:nas:/": "/ on nas is almost full"}
    clock.now += QUIET_AFTER + 1
    assert list(alerter.problems()) == ["companion:nas"]
    reg = registry(rt)
    assert reg.get_sample_value("argus_companion_disk_used_ratio",
                                {"companion": "nas", "mount": "/"}) == 0.96
    assert reg.get_sample_value("argus_companion_last_report_timestamp_seconds",
                                {"companion": "nas"}) is not None


def test_checks_for_a_companion_are_not_run_by_argus(rt):
    rt.tick()
    assert "nas-web" not in rt.next_due and "outside" in rt.next_due


def test_install_script_and_missing_builds(client):
    r = client.get("/companion/install.sh")
    assert r.status_code == 200 and r.text.startswith("#!/bin/sh")
    assert client.get("/companion/download/linux-amd64").status_code == 404
    assert client.get("/companion/download/../../etc/passwd").status_code == 404


def test_host_names_become_companion_names():
    assert name_from("NAS01.home.lan") == "nas01"
    assert name_from("my_box") == "my-box"
    assert name_from("___") == "host"
