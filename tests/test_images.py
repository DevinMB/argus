"""A container that runs an older image than its registry now serves."""

import asyncio
import sqlite3

import httpx

from argus.images import Images, parse

OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def test_only_named_public_registries_are_asked():
    assert parse("postgres:16") == ("registry-1.docker.io", "library/postgres", "16")
    assert parse("jellyfin/jellyfin") == ("registry-1.docker.io", "jellyfin/jellyfin", "latest")
    assert parse("ghcr.io/immich-app/immich-server:v2") == (
        "ghcr.io", "immich-app/immich-server", "v2")
    # Pinned by digest, a registry of the owner's, or an address a compromised
    # host could aim Argus at: never asked.
    assert parse("postgres@" + OLD) is None
    assert parse("registry.lan:5000/app:1") is None
    assert parse("192.168.1.1/admin:x") is None
    assert parse("localhost/app") is None
    assert parse("ghcr.io/../etc:1") is None
    assert parse("") is None


def images(handler, wanted, now=lambda: 1000.0):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Images(sqlite3.connect(":memory:"), client, now, lambda: wanted)


def test_a_moved_tag_shows_as_behind_and_a_private_image_is_not_known():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.method, request.url.host, request.url.path))
        if request.url.host == "auth.docker.io":
            assert request.url.params["scope"] == "repository:library/postgres:pull"
            return httpx.Response(200, json={"token": "anon"})
        if request.url.host == "registry-1.docker.io":
            if request.headers.get("authorization") != "Bearer anon":
                return httpx.Response(401, headers={"www-authenticate": (
                    'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"')})
            return httpx.Response(200, headers={"docker-content-digest": NEW})
        if request.url.path.startswith("/v2/me/evil"):
            # A registry that sends Argus elsewhere for a token is not followed.
            return httpx.Response(401, headers={"www-authenticate": (
                'Bearer realm="https://192.168.1.1/token",service="x"')})
        return httpx.Response(401)  # private: no anonymous access

    im = images(handler, {"postgres:16", "ghcr.io/me/private:1", "ghcr.io/me/evil:1",
                          "registry.lan:5000/app:1"})
    asyncio.run(im.refresh())
    assert im.behind("postgres:16", [OLD])
    assert not im.behind("postgres:16", [NEW, OLD])  # it runs what the registry serves
    assert not im.behind("postgres:16", [])  # built here: nothing to compare
    assert not im.behind("ghcr.io/me/private:1", [OLD])
    assert not im.behind("ghcr.io/me/evil:1", [OLD])
    hosts = {h for _, h, _ in asked}
    assert hosts == {"registry-1.docker.io", "auth.docker.io", "ghcr.io"}
    assert all(m == "HEAD" for m, h, _ in asked if h != "auth.docker.io")  # no pulls
    # Asked once per image every six hours; what was known stays through an outage.
    n = len(asked)
    asyncio.run(im.refresh())
    assert len(asked) == n


def test_what_was_known_survives_a_restart_and_an_outage(tmp_path):
    db = sqlite3.connect(tmp_path / "a.db")
    t = [1000.0]
    ok = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"docker-content-digest": NEW})))
    im = Images(db, ok, lambda: t[0], lambda: {"ghcr.io/a/b:1"})
    asyncio.run(im.refresh())
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    again = Images(db, down, lambda: t[0], lambda: {"ghcr.io/a/b:1"})
    assert again.behind("ghcr.io/a/b:1", [OLD])
    t[0] += 7 * 3600
    asyncio.run(again.refresh())
    assert again.behind("ghcr.io/a/b:1", [OLD])
    # An image no host runs any more is forgotten.
    again.wanted = lambda: set()
    asyncio.run(again.refresh())
    assert again.known == {}
