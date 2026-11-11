import logging
import re
import sqlite3

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient

from argus.app import create_app
from argus.auth import COOKIE, Auth
from argus.runtime import Runtime
from argus.settings import Settings, load_key

PASSWORD = "correct horse battery"  # noqa: S105 (a test password)


@pytest.fixture
def rt(config_file, tmp_path, prober, clock):
    return Runtime(config_file, tmp_path / "argus.db", prober=prober, clock=clock)


@pytest.fixture
def client(rt, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c


def setup_code(caplog) -> str:
    logged = [r.getMessage() for when in ("setup", "call") for r in caplog.get_records(when)]
    return re.findall(r"Setup code: (\S+)", "\n".join(logged))[-1]


def sign_up(client, caplog) -> dict:
    r = client.post("/api/setup", json={"code": setup_code(caplog), "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def test_settings_secrets_are_sealed_and_bound_to_their_name(tmp_path):
    db = sqlite3.connect(tmp_path / "x.db")
    key = load_key(tmp_path / "argus.key")
    assert (tmp_path / "argus.key").stat().st_mode & 0o777 == 0o600
    assert load_key(tmp_path / "argus.key") == key  # made once
    s = Settings(db, key)
    s.set_secret("a", "hunter2-token")
    assert s.secret("a") == "hunter2-token" and s.has_secret("a")
    raw = db.execute("SELECT sealed FROM secrets").fetchone()[0]
    assert "hunter2" not in raw
    # Moved into another slot, it no longer decrypts.
    db.execute("INSERT INTO secrets VALUES ('b', ?)", (raw,))
    with pytest.raises(InvalidTag):
        s.secret("b")
    s.set_secret("a", "")
    assert s.secret("a") is None
    s.set("x", {"y": [1]})
    assert s.get("x") == {"y": [1]} and s.get("nope", 3) == 3


def test_a_fresh_install_needs_the_logged_setup_code(client, caplog):
    assert client.get("/api/session").json() == {
        "setup_needed": True, "signed_in": False, "csrf": None, "view_requires_login": False}
    r = client.post("/api/setup", json={"code": "0000-0000-0000", "password": PASSWORD})
    assert r.status_code == 403
    r = client.post("/api/setup", json={"code": setup_code(caplog), "password": "short"})
    assert r.status_code == 422
    s = sign_up(client, caplog)
    assert s["signed_in"] and s["csrf"] and not s["setup_needed"]
    cookie = client.cookies.get(COOKIE)
    assert cookie
    # Once set, the setup route is closed.
    again = client.post("/api/setup", json={"code": setup_code(caplog), "password": PASSWORD})
    assert again.status_code == 409


def test_cookie_flags(client, caplog):
    r = client.post("/api/setup", json={"code": setup_code(caplog), "password": PASSWORD})
    c = r.headers["set-cookie"].lower()
    assert "httponly" in c and "samesite=strict" in c


def test_writes_need_the_session_and_its_csrf_token(client, caplog):
    csrf = sign_up(client, caplog)["csrf"]
    body = {"repeat_minutes": 30}
    assert client.put("/api/alerts", json=body).status_code == 403  # no token
    assert client.put("/api/alerts", json=body,
                      headers={"X-CSRF-Token": "wrong"}).status_code == 403
    r = client.put("/api/alerts", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["repeat_minutes"] == 30
    client.cookies.clear()
    r = client.put("/api/alerts", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 401 and "default-src" in r.headers["content-security-policy"]


def test_login_logout_and_rate_limit(client, caplog):
    csrf = sign_up(client, caplog)["csrf"]
    client.post("/api/logout", headers={"X-CSRF-Token": csrf})
    assert not client.get("/api/session").json()["signed_in"]
    assert client.post("/api/login", json={"password": PASSWORD}).json()["signed_in"]
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope nope nope"}).status_code == 403
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_password_change_signs_out_other_sessions(rt, client, caplog):
    csrf = sign_up(client, caplog)["csrf"]
    other = client.cookies.get(COOKIE)
    r = client.post("/api/password", headers={"X-CSRF-Token": csrf},
                    json={"current": PASSWORD, "new": "another long password"})
    assert r.status_code == 200
    auth = client.app.state.auth
    assert auth.session(other) is None
    assert auth.session(client.cookies.get(COOKIE)) is not None


def test_a_new_install_requires_login_to_view_and_the_owner_can_open_it(client, caplog):
    # The pages list each host's address and unpatched CVEs: not for every
    # device on the network unless the owner says so.
    csrf = sign_up(client, caplog)["csrf"]
    assert client.get("/api/checks").status_code == 200
    jar = dict(client.cookies)
    client.cookies.clear()
    assert client.get("/api/checks").status_code == 401
    assert client.get("/api/companions").status_code == 401
    assert client.get("/api/session").json()["view_requires_login"] is True
    client.cookies.update(jar)
    client.put("/api/access", json={"view_requires_login": False}, headers={"X-CSRF-Token": csrf})
    client.cookies.clear()
    assert client.get("/api/checks").status_code == 200
    client.cookies.update(jar)
    client.put("/api/access", json={"view_requires_login": True}, headers={"X-CSRF-Token": csrf})
    client.cookies.clear()
    assert client.get("/api/checks").status_code == 401
    # Timers, health and metrics stay open.
    assert client.post("/heartbeat/stack-sync").status_code == 204
    assert client.get("/healthz").status_code in (200, 503)
    assert client.get("/metrics").status_code == 200


def test_reset_login_prints_a_new_code(rt, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    s = Settings(rt.store.db, load_key(tmp_path / "argus.key"))
    a = Auth(rt.store.db, s, rt.clock)
    first = a.setup_code()
    a.setup(first, PASSWORD, "1.2.3.4")
    assert a.setup_code() is None
    a.reset()
    assert a.needs_setup() and a.setup_code() not in (None, first)
