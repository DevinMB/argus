"""Whether a container runs an older image than its registry now serves.

A host reports each container's image ("jellyfin/jellyfin:10.11") and the
registry digest of the copy it runs. Argus asks the registry, once per image
every few hours, which digest that tag points at now. When it differs, the tag
has moved: a newer image is out, and the container still runs the old one.

That is all it says. A tag that never moves (a full version, "2.19.1") never
shows as behind; finding a newer *version* is Dependabot's work on a stack kept
in git. An image built on the host, or pinned by digest, is left alone.

Argus asks itself, so a host needs no internet for this. A companion names the
image, so only well-known public registries are asked (never an address a
compromised host could aim Argus at), with no account: a private image is
simply not known. A HEAD on a manifest does not count against Docker Hub's
pull limit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("argus.images")

EVERY = 600  # how often the loop looks for work
AGAIN = 6 * 3600  # how often one image is asked about
PER_PASS = 30
SOON = 60
# registry in the image name -> the host that answers for it
REGISTRIES = {
    "docker.io": "registry-1.docker.io", "index.docker.io": "registry-1.docker.io",
    "registry-1.docker.io": "registry-1.docker.io", "ghcr.io": "ghcr.io", "quay.io": "quay.io",
    "lscr.io": "lscr.io", "gcr.io": "gcr.io", "registry.k8s.io": "registry.k8s.io",
    "mcr.microsoft.com": "mcr.microsoft.com", "public.ecr.aws": "public.ecr.aws",
    "registry.gitlab.com": "registry.gitlab.com", "codeberg.org": "codeberg.org",
    "docker.elastic.co": "docker.elastic.co",
}
# who may hand out a registry's anonymous token
TOKEN_HOSTS = {*REGISTRIES.values(), "auth.docker.io", "gitlab.com"}
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json"])
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
CHALLENGE = re.compile(r'(\w+)="([^"]*)"')

SCHEMA = """
CREATE TABLE IF NOT EXISTS image_digests (
    image TEXT PRIMARY KEY, digest TEXT, checked REAL NOT NULL
);
"""


def parse(image: str) -> tuple[str, str, str] | None:
    """(registry host, repository, tag) for an image Argus may ask about, or
    None: pinned by digest, an unknown or private registry, or not a name."""
    if not image or "@" in image:
        return None
    name, tag = image, "latest"
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = image.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    host = REGISTRIES.get(registry)
    if host is None or not REPO.match(repo) or not TAG.match(tag):
        return None
    if host == "registry-1.docker.io" and "/" not in repo:
        repo = "library/" + repo
    return host, repo, tag


class Images:
    def __init__(self, db: sqlite3.Connection, client: httpx.AsyncClient,
                 clock: Callable[[], float], wanted: Callable[[], Iterable[str]]) -> None:
        self.db, self.client, self.clock = db, client, clock
        self.wanted = wanted  # the image names running on any host now
        with self.db:
            self.db.executescript(SCHEMA)
        self.known: dict[str, tuple[str | None, float]] = {
            r[0]: (r[1], r[2])
            for r in self.db.execute("SELECT image, digest, checked FROM image_digests")}

    def behind(self, image: str, digests: list[str] | None) -> bool:
        """The registry serves another image under this tag than the one running."""
        remote = self.known.get(image, (None, 0))[0]
        return bool(remote and digests and remote not in digests)

    async def remote(self, host: str, repo: str, tag: str) -> str | None:
        """The digest the registry serves for repo:tag now, asked without an account."""
        url = f"https://{host}/v2/{repo}/manifests/{tag}"
        r = await self.client.head(url, headers={"Accept": ACCEPT}, timeout=15)
        if r.status_code == 401:
            ask = dict(CHALLENGE.findall(r.headers.get("www-authenticate", "")))
            realm = urlsplit(ask.get("realm", ""))
            if realm.scheme != "https" or realm.hostname not in TOKEN_HOSTS:
                return None
            t = await self.client.get(ask["realm"], timeout=15, params={
                "service": ask.get("service", ""), "scope": f"repository:{repo}:pull"})
            if t.status_code != 200:
                return None
            body = t.json()
            token = body.get("token") or body.get("access_token")
            if not isinstance(token, str):
                return None
            r = await self.client.head(url, timeout=15, headers={
                "Accept": ACCEPT, "Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return None  # private, gone, or not saying: not known
        digest = r.headers.get("docker-content-digest", "")
        return digest if DIGEST.match(digest) else None

    async def refresh(self) -> None:
        now = self.clock()
        wanted = {i for i in self.wanted() if parse(i)}
        due = sorted(i for i in wanted
                     if i not in self.known or now - self.known[i][1] >= AGAIN)
        for image in due[:PER_PASS]:
            try:
                digest = await self.remote(*parse(image))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                digest = None
            # What was known stays when the registry does not answer.
            digest = digest or self.known.get(image, (None, 0))[0]
            self.known[image] = (digest, now)
            with self.db:
                self.db.execute(
                    "INSERT INTO image_digests (image, digest, checked) VALUES (?, ?, ?)"
                    " ON CONFLICT(image) DO UPDATE SET digest = excluded.digest,"
                    " checked = excluded.checked", (image, digest, now))
            await asyncio.sleep(0.3)
        gone = [i for i in self.known if i not in wanted]
        if gone:
            for i in gone:
                del self.known[i]
            with self.db:
                self.db.executemany("DELETE FROM image_digests WHERE image = ?",
                                    [(i,) for i in gone])

    async def run_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - a lookup must never end the loop
                log.exception("image lookup failed")
            # After a start the hosts have not reported yet: look again soon, so the
            # first answers do not take a whole round to appear.
            await asyncio.sleep(EVERY if self.known else SOON)
