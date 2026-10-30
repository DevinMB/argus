import base64
import hashlib
import json
import logging
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus.adopt import inline_secrets, parse_env
from argus.app import create_app
from argus.deploys import WRITE_TOKEN
from argus.github import Poller
from argus.runtime import Runtime

WRITE = "github_pat_" + "W" * 30
COMPOSE = "services:\n  app:\n    image: jellyfin/jellyfin\n    env_file: .env\n"


class FakeRepo:
    """ann/stacks: files by path, one branch, commits that fast-forward."""

    def __init__(self):
        self.files = {"hosts/nas": "# what nas runs\nsites/nas/old\n",
                      "sites/nas/old/compose.yml": "services: {old: {image: busybox}}\n"}
        self.head, self.commits = "a" * 40, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        base = "/repos/ann/stacks"
        if m != "GET":
            assert request.headers["authorization"] == f"Bearer {WRITE}"
            body = json.loads(request.content)
            if path.endswith("/git/trees"):
                self.pending = {e["path"]: e["content"] for e in body["tree"]}
                return httpx.Response(201, json={"sha": "t"})
            if path.endswith("/git/commits"):
                self.commits.append((body["message"], dict(self.pending)))
                return httpx.Response(201, json={"sha": "b" * 40})
            if path.endswith("/git/refs/heads/main"):
                self.files.update(self.pending)
                self.head = "b" * 40
                return httpx.Response(200, json={})
        blob = lambda p: hashlib.sha256(self.files[p].encode()).hexdigest()  # noqa: E731
        if path == base:
            return httpx.Response(200, json={"default_branch": "main"})
        if path == f"{base}/commits/main" or path == f"{base}/git/ref/heads/main":
            return httpx.Response(200, json={"sha": self.head, "object": {"sha": self.head}})
        if path == f"{base}/git/trees/{self.head}":
            return httpx.Response(200, json={"tree": [
                {"path": p, "type": "blob", "sha": blob(p), "size": len(t)}
                for p, t in self.files.items()]})
        if path.startswith(f"{base}/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            text = next(t for p, t in self.files.items() if blob(p) == sha)
            return httpx.Response(200, json={"content": base64.b64encode(text.encode()).decode()})
        if path == f"{base}/git/commits/{self.head}":
            return httpx.Response(200, json={"tree": {"sha": "t0"}})
        return httpx.Response(404, json={})


@pytest.fixture
def env(tmp_path, prober, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    fake = FakeRepo()
    rt = Runtime(tmp_path / "absent.yml", tmp_path / "argus.db", prober=prober)
    poller = Poller("read", transport=httpx.MockTransport(fake))
    with TestClient(create_app(rt, start_scheduler=False, poller=poller)) as c:
        csrf = {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}
        s = c.app.state.settings
        s.set("deploy.repo", "ann/stacks")
        s.set_secret(WRITE_TOKEN, WRITE)
        c.portal.call(c.app.state.deploys.refresh)
        code = c.post("/api/companions/codes", headers=csrf,
                      json={"url": "https://argus.lan", "name": "nas"}).json()["code"]
        tok = c.post("/companion/enroll", json={"code": code, "hostname": "nas"}).json()["token"]
        yield c, csrf, tok, fake


def report(c, tok, **fields):
    base = {"nonce": secrets.token_urlsafe(16), "policy_roots": ["/srv", "/opt"],
            "containers": [{"name": "jellyfin", "state": "running", "project": "media",
                            "working_dir": "/srv/media",
                            "config_files": ["/srv/media/compose.yml"]}]}
    return c.post("/companion/report", headers={"Authorization": f"Bearer {tok}"},
                  json={**base, **fields}).json()


def collected(text=COMPOSE, env="# media\nDB_PASSWORD='hunter2 x'\nexport TZ=UTC\n"):
    return [{"project": "media", "dir": "/srv/media", "env": env,
             "files": {"compose.yml": base64.b64encode(text.encode()).decode()}}]


def test_adopting_in_place_commits_files_and_seals_secrets(env):
    c, csrf, tok, fake = env
    report(c, tok)
    (host,) = c.get("/api/companions").json()["companions"]
    assert host["adoptable"] == [{"project": "media", "dir": "/srv/media",
                                  "containers": ["jellyfin"]}]
    assert c.post("/api/adopt/nas/media/read", headers=csrf).json()["state"] == "collecting"
    assert report(c, tok)["collect"] == ["media"]  # Argus names the project, nothing more
    assert "collect" not in report(c, tok, collected=collected())

    pv = c.get("/api/adopt/nas/media", headers=csrf).json()
    assert pv["state"] == "ready" and pv["blockers"] == []
    assert pv["path"] == "sites/nas/media" and pv["dir"] == "/srv/media"
    assert pv["secrets"] == ["DB_PASSWORD", "TZ"] and "hunter2" not in json.dumps(pv)
    rows = c.app.state.rt.store.db.execute("SELECT report FROM companions").fetchall()
    assert "hunter2" not in str([r[0] for r in rows])  # never stored

    assert c.post("/api/adopt/nas/media", headers=csrf).status_code == 200
    msg, files = fake.commits[-1]
    assert msg.startswith("Adopt media on nas")
    assert files == {"sites/nas/media/compose.yml": COMPOSE,
                     "hosts/nas": "# what nas runs\nsites/nas/old\nsites/nas/media\n"}
    assert "hunter2" not in json.dumps(fake.commits)

    stacks = {s["path"]: s for s in c.app.state.deploys.stacks_for("nas")}
    media = stacks["sites/nas/media"]
    assert media["dir"] == "/srv/media" and "DB_PASSWORD='hunter2 x'" in media["env"]
    assert "dir" not in stacks["sites/nas/old"]
    (host,) = c.get("/api/companions").json()["companions"]
    assert host["adoptable"] == []  # git deploys it now


def test_what_blocks_an_adoption_says_how_to_fix_it(env):
    c, csrf, tok, fake = env
    report(c, tok)
    c.post("/api/adopt/nas/media/read", headers=csrf)
    inline = COMPOSE + "    environment:\n      DB_PASSWORD: hunter2\n      API_KEY: ${API_KEY}\n"
    report(c, tok, policy_roots=["/opt"], collected=collected(text=inline))
    pv = c.get("/api/adopt/nas/media", headers=csrf).json()
    texts = " | ".join(b["text"] for b in pv["blockers"])
    assert "writes DB_PASSWORD into itself" in texts and "API_KEY" not in texts
    assert "outside the folders this host lets Argus manage (/opt)" in texts
    r = c.post("/api/adopt/nas/media", headers=csrf)
    assert r.status_code == 409 and fake.commits == []


def test_monitoring_only_hosts_and_strangers_get_nothing(env):
    c, csrf, tok, fake = env
    assert c.post("/api/adopt/nas/nothere/read", headers=csrf).status_code == 404
    assert c.post("/api/adopt/nas/media/read").status_code == 403  # no CSRF token


def test_env_and_inline_secret_parsing():
    env, bad = parse_env("A=1\n# c\nexport B='two words'\nC=\"x=y\"\nnot a line\n")
    assert env == {"A": "1", "B": "two words", "C": "x=y"} and len(bad) == 1
    assert inline_secrets("x:\n  DB_PASSWORD: s3cret\n  TOKEN: ${TOKEN}\n"
                          "  - GH_TOKEN=abc\n") == ["DB_PASSWORD", "GH_TOKEN"]
