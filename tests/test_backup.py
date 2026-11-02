import json
import logging
import secrets

import pytest
from fastapi.testclient import TestClient
from test_auth import PASSWORD, sign_up

from argus import configsrc
from argus.alerts import TOKEN
from argus.app import create_app
from argus.runtime import Runtime

BOT = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef"


def install(path, prober, caplog):
    path.mkdir()
    rt = Runtime(path / "absent.yml", path / "argus.db", prober=prober)
    return TestClient(create_app(rt, start_scheduler=False, tls_pin=None))


@pytest.fixture
def pair(tmp_path, prober, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    with install(tmp_path / "old", prober, caplog) as old:
        csrf = {"X-CSRF-Token": sign_up(old, caplog)["csrf"]}
        yield old, csrf, tmp_path, prober, caplog


def test_an_export_carries_secrets_hosts_and_the_signing_key_to_a_new_install(pair):
    old, csrf, tmp_path, prober, caplog = pair
    old.put("/api/alerts", headers=csrf, json={"telegram_token": BOT, "chat_id": "42"})
    code = old.post("/api/companions/codes", headers=csrf,
                    json={"url": "https://argus.lan", "name": "nas"}).json()["code"]
    me = old.post("/companion/enroll", json={"code": code, "hostname": "nas"}).json()
    (tmp_path / "old" / "tls.key").write_text("KEY")
    # A paused check goes along; the restored config still has the check.
    config = "checks:\n  - {name: ollama, kind: ping, target: h}\n"
    old.app.state.settings.set(configsrc.TEXT, config)
    old.app.state.rt.store.pause("ollama", "off for now", "resumed", 100.0)

    # Needs the password again, and a real passphrase.
    assert old.post("/api/export", headers=csrf, json={
        "password": "wrong password!", "passphrase": "a long passphrase"}).status_code == 403
    assert old.post("/api/export", headers=csrf, json={
        "password": PASSWORD, "passphrase": "short"}).status_code == 422
    r = old.post("/api/export", headers=csrf, json={
        "password": PASSWORD, "passphrase": "a long passphrase"})
    assert r.status_code == 200 and "argus-export-" in r.headers["content-disposition"]
    blob = r.text
    assert BOT not in blob and "KEY" not in blob and me["token"] not in blob  # sealed
    assert old.get("/api/start").json()["exported_at"] is not None

    caplog.clear()
    with install(tmp_path / "new", prober, caplog) as new:
        ncsrf = {"X-CSRF-Token": sign_up(new, caplog)["csrf"]}
        bad = new.post("/api/import", headers=ncsrf, json={"data": blob, "passphrase": "nope"})
        assert bad.status_code == 400 and "wrong passphrase" in bad.json()["detail"]
        out = new.post("/api/import", headers=ncsrf,
                       json={"data": blob, "passphrase": "a long passphrase"}).json()
        assert out == {"hosts": 1, "secrets": 2, "restart": True, "signed_out": True}
        assert new.app.state.settings.secret(TOKEN) == BOT
        assert (tmp_path / "new" / "tls.key").read_text() == "KEY"
        assert new.app.state.rt.store.pause_of("ollama")["reason"] == "off for now"
        # The host carries on: same token, replies signed with the same key.
        rep = new.post("/companion/report", headers={"Authorization": f"Bearer {me['token']}"},
                       json={"nonce": secrets.token_urlsafe(16)})
        assert rep.status_code == 200
        assert new.app.state.signer.public == me["public_key"]
        # Signed out: the exported install's password is the one now.
        assert new.post("/api/login", json={"password": PASSWORD}).status_code == 200
        assert json.loads(blob)["format"] == "argus-export-v1"


def test_export_and_import_are_admin_writes(pair):
    old, csrf, *_ = pair
    assert old.post("/api/export", json={"password": PASSWORD,
                                         "passphrase": "a long passphrase"}).status_code == 403
    assert old.post("/api/import", json={"data": "{}", "passphrase": "x"}).status_code == 403
