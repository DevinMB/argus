import base64
import json
import logging
import re
import textwrap

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus import ghauth
from argus.app import create_app
from argus.github import Poller, is_major
from argus.runtime import Runtime

NOW = 1_800_000_000.0
ISO = "2027-01-15T08:00:00Z"
PAT = "github_pat_" + "A1" * 20
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()


def repo(owner, name, **kw):
    return {"full_name": f"{owner}/{name}", "name": name, "owner": {"login": owner},
            "default_branch": kw.pop("branch", "main"), "private": True,
            "html_url": f"https://github.com/{owner}/{name}", **kw}


class FakeGitHub:
    """Enough of GitHub for discovery, the pipeline of one repo, and the app flow."""

    def __init__(self):
        self.calls = []
        self.token_ok = True
        self.repos = [repo("ann", "shop", branch="trunk"), repo("ann", "old", archived=True),
                      repo("ann", "notes")]

    def jwt_ok(self, request):
        head, body, sig = request.headers["authorization"].removeprefix("Bearer ").split(".")
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        KEY.public_key().verify(pad(sig), f"{head}.{body}".encode(), padding.PKCS1v15(),
                                hashes.SHA256())
        claims = json.loads(pad(body))
        assert claims["iss"] == "77" and claims["exp"] - claims["iat"] <= 600
        return True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, auth = request.url.path, request.headers.get("authorization", "")
        self.calls.append((request.method, path, auth, request.headers.get("if-none-match")))
        if path == "/app-manifests/c0de/conversions":
            return httpx.Response(201, json={
                "id": 77, "slug": "argus-ann", "name": "Argus ann", "pem": PEM,
                "html_url": "https://github.com/apps/argus-ann",
                "owner": {"login": "ann", "type": "User"}})
        if path == "/app/installations" and self.jwt_ok(request):
            return httpx.Response(200, json=[{"id": 5, "account": {"login": "ann"},
                                              "html_url": "https://github.com/settings/i/5",
                                              "repository_selection": "selected"}])
        if path == "/app/installations/5/access_tokens" and self.jwt_ok(request):
            return httpx.Response(201, json={"token": "ghs_install"})
        if path == "/installation/repositories":
            assert auth == "Bearer ghs_install"
            return httpx.Response(200, json={"repositories": self.repos})
        if auth not in (f"Bearer {PAT}", "Bearer ghs_install") or not self.token_ok:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/user/repos":
            return httpx.Response(200, json=self.repos)
        if path == "/repos/ann/shop/pulls":
            if request.url.params["state"] == "open":
                if request.headers.get("if-none-match") == '"open-1"':
                    return httpx.Response(304)
                return httpx.Response(200, headers={"etag": '"open-1"'}, json=[
                    {"number": 4, "title": "Bump react from 19.2.0 to 20.0.0 in /ui",
                     "html_url": "u4", "created_at": ISO, "user": {"login": "dependabot[bot]"},
                     "head": {"sha": "p4"}, "labels": []}])
            return httpx.Response(200, json=[])
        if path == "/repos/ann/shop/actions/runs" and request.url.params.get("branch") == "trunk":
            # No ci.yml here: the newest push build on the default branch counts.
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 9, "event": "push", "status": "completed", "conclusion": "success",
                 "head_sha": "abcdef1234", "html_url": "run9", "created_at": ISO,
                 "updated_at": ISO, "head_commit": {"message": "Add search (#3)"}}]})
        if path == "/repos/ann/shop/actions/runs":
            return httpx.Response(200, json={"workflow_runs": [
                {"status": "completed", "conclusion": "success"}]})
        if path == "/repos/ann/shop/actions/runs/9/jobs":
            return httpx.Response(200, json={"jobs": []})
        if path == "/repos/ann/shop/pulls/3":
            return httpx.Response(200, json={"merged_by": {"login": "ann"}})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.mark.parametrize("title,major", [
    ("Bump react from 19.2.0 to 20.0.0 in /ui", True),
    ("Bump httpx from 0.27.2 to 0.28.0", True),
    ("Bump httpx from 0.28.0 to 0.28.1", False),
    ("Bump fastapi from 0.115.0 to 0.115.6", False),
    ("Bump actions/checkout from v6 to v7", True),
    ("Bump the updates group with 3 updates", False),
    ("Bump node from 22.11.0-alpine to 22.12.0-alpine", False),
])
def test_majors_are_read_from_dependabot_titles(title, major):
    assert is_major(title) is major


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def rt(tmp_path, prober):
    # A fresh install: no argus.yml at all.
    return Runtime(tmp_path / "none.yml", tmp_path / "argus.db", prober=prober,
                   clock=lambda: NOW)


@pytest.fixture
def client(rt, fake, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    gh_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    poller = Poller(None, clock=lambda: NOW, transport=httpx.MockTransport(fake))
    with TestClient(create_app(rt, start_scheduler=False, poller=poller,
                               github_client=gh_client)) as c:
        yield c


async def poll_once(client):
    gh, r = client.app.state.gh, client.app.state.rt
    targets = await gh.targets_for(r.config, set(client.app.state.settings.get(ghauth.HIDDEN, [])))
    await gh.poll(r.config.github, targets)


async def test_a_fresh_install_with_a_token_shows_its_repos_pipeline(client, fake, caplog):
    csrf = {"X-CSRF-Token": sign_up(client, caplog)["csrf"]}
    assert client.get("/api/updates").json()["enabled"] is False
    fake.token_ok = False
    r = client.put("/api/github", json={"token": PAT}, headers=csrf)
    assert r.status_code == 400 and "refused" in r.json()["detail"]
    assert client.get("/api/github").json()["token_set"] is False  # a bad token is not kept
    fake.token_ok = True
    view = client.put("/api/github", json={"token": PAT}, headers=csrf).json()
    assert view["source"] == "token" and view["token_set"]
    assert [x["full_name"] for x in view["repos"]] == ["ann/notes", "ann/old", "ann/shop"]
    assert PAT not in json.dumps(view)

    client.put("/api/github", json={"hidden": ["ann/notes"]}, headers=csrf)
    await poll_once(client)
    u = client.get("/api/updates").json()
    assert u["enabled"] and u["owners"] == ["ann"]
    (shop,) = u["repos"]  # archived and hidden ones are left out
    assert shop["name"] == "shop" and shop["branch"] == "trunk"
    assert shop["updates"][0]["waits_for_you"]  # a major, read from the title
    steps = {s["key"]: s for s in shop["track"]}
    assert steps["updates"]["status"] == "waiting"
    assert steps["merged"]["detail"] == "Add search (#3, merged by ann)"
    assert steps["built"]["status"] == "done"


async def test_unchanged_answers_come_from_the_etag_cache(client, fake, caplog):
    csrf = {"X-CSRF-Token": sign_up(client, caplog)["csrf"]}
    client.put("/api/github", json={"token": PAT}, headers=csrf)
    await poll_once(client)
    await poll_once(client)
    opens = [c for c in fake.calls if c[1] == "/repos/ann/shop/pulls" and c[3]]
    assert opens and opens[-1][3] == '"open-1"'
    shop = next(r for r in client.get("/api/updates").json()["repos"] if r["name"] == "shop")
    assert shop["updates"][0]["number"] == 4  # the cached answer, not an empty one


async def test_connect_github_makes_an_app_and_uses_installation_tokens(client, fake, caplog):
    csrf = {"X-CSRF-Token": sign_up(client, caplog)["csrf"]}
    m = client.post("/api/github/manifest", headers=csrf,
                    json={"url": "https://argus.lan:8443", "name": "Argus ann"}).json()
    assert m["action"].startswith("https://github.com/settings/apps/new?state=")
    manifest = json.loads(m["manifest"])
    assert manifest["redirect_url"] == "https://argus.lan:8443/github/manifest-callback"
    assert set(manifest["default_permissions"].values()) == {"read"}
    assert manifest["public"] is False
    state = re.search(r"state=(\S+)", m["action"]).group(1)

    # A forged or replayed callback changes nothing.
    r = client.get("/github/manifest-callback", params={"code": "c0de", "state": "forged"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#/github"
    assert "press Connect GitHub again" in client.get("/api/github").json()["connect_error"]
    assert not any(c[1].startswith("/app-manifests") for c in fake.calls)

    m = client.post("/api/github/manifest", headers=csrf,
                    json={"url": "https://argus.lan:8443", "name": "Argus ann"}).json()
    state = re.search(r"state=(\S+)", m["action"]).group(1)
    r = client.get("/github/manifest-callback", params={"code": "c0de", "state": state},
                   follow_redirects=False)
    assert r.headers["location"] == "https://github.com/apps/argus-ann/installations/new"
    again = client.get("/github/manifest-callback", params={"code": "c0de", "state": state},
                       follow_redirects=False)
    assert "again" in client.get("/api/github").json()["connect_error"]  # single use
    assert again.status_code == 303

    s = client.app.state.settings
    assert s.get(ghauth.APP)["slug"] == "argus-ann" and s.secret(ghauth.APP_KEY) == PEM
    await client.app.state.gh.discover(force=True)
    view = client.get("/api/github").json()
    assert view["source"] == "app" and view["installations"][0]["account"] == "ann"
    assert view["settings_url"] == "https://github.com/settings/apps/argus-ann"
    assert "BEGIN" not in json.dumps(view)  # the key is never shown
    await poll_once(client)
    assert client.get("/api/updates").json()["repos"][0]["name"] == "notes"
    assert any(c[2] == "Bearer ghs_install" for c in fake.calls if c[1].startswith("/repos/"))

    # A pasted token cannot sneak in beside the app; disconnecting forgets both.
    assert client.put("/api/github", json={"token": PAT}, headers=csrf).status_code == 409
    view = client.post("/api/github/disconnect", headers=csrf).json()
    assert view["source"] == "none" and s.secret(ghauth.APP_KEY) is None
    assert client.get("/api/updates").json()["enabled"] is False


def test_the_github_page_needs_the_admin(client):
    assert client.get("/api/github").status_code == 401
    assert client.post("/api/github/manifest",
                       json={"url": "https://a", "name": "Argus"}).status_code == 401


def test_owners_that_mix_show_full_names(tmp_path):
    import asyncio

    from argus import config as cfg

    class Creds:
        async def token_for(self, owner):
            return "t"

        async def repos(self):
            return [ghauth._repo(repo("ann", "web")), ghauth._repo(repo("acme", "web"))]

        def describe(self):
            return {}

    p = Poller(Creds(), clock=lambda: NOW)
    c = cfg.Config.model_validate(yaml_load("repos: [{name: acme/web, check: site}]\n"
                                            "checks: [{name: site, kind: ping, target: h}]"))
    targets = asyncio.run(p.targets_for(c, set()))
    assert [(t.name, t.repo.check) for t in targets] == [("acme/web", "site"), ("ann/web", "")]


def yaml_load(text):
    import yaml
    return yaml.safe_load(textwrap.dedent(text))
