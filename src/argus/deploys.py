"""Deploys: what each host should run, from the deploy repo, sent to its companion.

The deploy repo keeps the layout this homelab's stacks repo already has:

    hosts/<host>            one stack folder per line ("sites/home/shop"); # comments
    sites/home/shop/...     the stack: compose.yml and any files next to it

`<host>` is the companion's name. Argus reads the repo on its default branch
every minute (a folder works too, ARGUS_DEPLOY_DIR, for tests and for people
without GitHub) and puts each host's stacks in the signed reply to its
companion: the files, the stack's secrets as its .env, and a digest. The
companion deploys a stack when its digest changes, after checking it against
the host's own policy, and reports how it went.

Secrets for stacks are kept sealed in Argus, set on the Hosts page, and only
ever sent to the host whose hosts/ file lists the stack.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from argus.ghauth import AppCredentials
from argus.settings import Settings

log = logging.getLogger("argus.deploys")

REPO = "deploy.repo"  # owner/name of the deploy repo
BRANCH = "deploy.branch"  # empty: its default branch
PULL_TOKEN = "deploy.pull_token"  # noqa: S105 (a setting name) secret: registry read token
SECRETS = "stack.env."  # + stack path: secret JSON {KEY: value}
MAX_FILE = 512 * 1024
MAX_STACK = 4 * 1024 * 1024
STACK = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def env_text(env: dict[str, str]) -> str:
    """A .env compose reads as-is: values single-quoted, so $ and # stay literal."""
    lines = []
    for k in sorted(env):
        v = env[k].replace("\n", "\\n")
        lines.append(f"{k}='{v}'" if "'" not in v else f'{k}="{v}"')
    return "".join(line + "\n" for line in lines)


@dataclass
class Snapshot:
    """The deploy repo at one commit: which host runs what, and the files."""

    commit: str
    hosts: dict[str, list[str]] = field(default_factory=dict)
    # stack path -> {relative file path: bytes}
    stacks: dict[str, dict[str, bytes]] = field(default_factory=dict)
    error: str = ""


def parse_hosts(name_to_text: dict[str, str]) -> dict[str, list[str]]:
    out = {}
    for name, text in name_to_text.items():
        stacks = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip().strip("/")
            if line and STACK.match(line) and ".." not in line.split("/"):
                stacks.append(line)
        out[name] = stacks
    return out


class DirSource:
    """A deploy repo checked out on disk (ARGUS_DEPLOY_DIR)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self) -> dict:
        return {"kind": "folder", "where": str(self.root)}

    async def snapshot(self) -> Snapshot:
        hosts_dir = self.root / "hosts"
        texts = {p.name: p.read_text() for p in sorted(hosts_dir.glob("*")) if p.is_file()} \
            if hosts_dir.is_dir() else {}
        hosts = parse_hosts(texts)
        stacks: dict[str, dict[str, bytes]] = {}
        for path in {s for v in hosts.values() for s in v}:
            d = self.root / path
            if d.is_dir():
                stacks[path] = {str(f.relative_to(d)): f.read_bytes()
                                for f in sorted(d.rglob("*"))
                                if f.is_file() and f.stat().st_size <= MAX_FILE}
        digest = hashlib.sha256(json.dumps(
            [hosts, {k: {n: hashlib.sha256(b).hexdigest() for n, b in v.items()}
                     for k, v in stacks.items()}], sort_keys=True).encode()).hexdigest()
        return Snapshot(digest[:12], hosts, stacks)


class GitHubSource:
    """The deploy repo on GitHub, read through the poller's connection. Trees and
    blobs are fetched once per sha; the branch head is an ETag request."""

    def __init__(self, poller, full_name: str, branch: str = "") -> None:
        self.poller, self.full_name, self.branch = poller, full_name, branch
        self._blobs: dict[str, bytes] = {}

    def describe(self) -> dict:
        return {"kind": "github", "repo": self.full_name, "branch": self.branch}

    async def snapshot(self) -> Snapshot:
        base = f"/repos/{self.full_name}"
        branch = self.branch
        if not branch:
            info = await self.poller._get(base)
            if info is None:
                return Snapshot("", error=f"{self.full_name} is not readable with this connection")
            branch = info["default_branch"]
        head = await self.poller._get(f"{base}/commits/{branch}")
        if head is None:
            return Snapshot("", error=f"no branch {branch} in {self.full_name}")
        sha = head["sha"]
        tree = await self.poller._get(f"{base}/git/trees/{sha}", recursive="1") or {}
        blobs = {t["path"]: t for t in tree.get("tree", []) if t["type"] == "blob"}

        async def read(entry) -> bytes | None:
            if entry["size"] > MAX_FILE:
                return None
            if entry["sha"] not in self._blobs:
                b = await self.poller._get(f"{base}/git/blobs/{entry['sha']}")
                self._blobs[entry["sha"]] = base64.b64decode(b["content"])
            return self._blobs[entry["sha"]]

        texts = {}
        for path, e in blobs.items():
            if path.startswith("hosts/") and path.count("/") == 1:
                data = await read(e)
                texts[path[6:]] = (data or b"").decode(errors="replace")
        hosts = parse_hosts(texts)
        stacks: dict[str, dict[str, bytes]] = {}
        for stack in {s for v in hosts.values() for s in v}:
            files = {}
            for path, e in blobs.items():
                if path.startswith(stack + "/"):
                    data = await read(e)
                    if data is not None:
                        files[path[len(stack) + 1:]] = data
            stacks[stack] = files
        return Snapshot(sha[:12], hosts, stacks)


class Deploys:
    def __init__(self, settings: Settings, poller, clock: Callable[[], float],
                 folder: Path | None = None) -> None:
        self.settings, self.poller, self.clock, self.folder = settings, poller, clock, folder
        self.snapshot: Snapshot | None = None
        self.read_at: float | None = None
        self.error = ""
        self._source = None
        self._key = None

    def source(self):
        if self.folder:
            return DirSource(self.folder)
        repo = self.settings.get(REPO)
        if not repo or not self.poller.connected:
            return None
        key = (repo, self.settings.get(BRANCH, ""), id(self.poller.creds))
        if key != self._key:  # keep the blob cache while nothing changed
            self._source, self._key = GitHubSource(self.poller, repo, key[1]), key
        return self._source

    async def refresh(self) -> None:
        src = self.source()
        if src is None:
            self.snapshot, self.error = None, ""
            return
        try:
            snap = await src.snapshot()
        except (httpx.HTTPError, KeyError, ValueError, OSError) as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            log.warning("reading the deploy repo failed, keeping the last: %s", self.error)
            return
        if snap.error:
            self.error = snap.error
            return
        self.snapshot, self.read_at, self.error = snap, self.clock(), ""

    async def run_forever(self, every: float = 60) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("deploy refresh crashed")
            await asyncio.sleep(every)

    # --- per host ---

    def env(self, stack: str) -> dict[str, str]:
        raw = self.settings.secret(SECRETS + stack)
        return json.loads(raw) if raw else {}

    def set_env(self, stack: str, changes: dict[str, str | None]) -> None:
        env = self.env(stack)
        for k, v in changes.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        self.settings.set_secret(SECRETS + stack, json.dumps(env) if env else None)

    def stacks_for(self, host: str) -> list[dict]:
        """What a host should run: each stack's files, .env and digest."""
        snap = self.snapshot
        if snap is None:
            return []
        out = []
        for path in snap.hosts.get(host, []):
            files = snap.stacks.get(path)
            if not files or "compose.yml" not in files and "compose.yaml" not in files \
                    and "docker-compose.yml" not in files:
                continue
            if sum(len(b) for b in files.values()) > MAX_STACK:
                continue
            env = self.env(path)
            digest = hashlib.sha256(json.dumps(
                [{n: hashlib.sha256(b).hexdigest() for n, b in files.items()}, env],
                sort_keys=True).encode()).hexdigest()[:16]
            out.append({"path": path, "name": path.rsplit("/", 1)[-1], "digest": digest,
                        "files": {n: base64.b64encode(b).decode() for n, b in files.items()},
                        "env": env_text(env)})
        return out

    async def pull_credential(self) -> dict | None:
        """Short-lived read access to the registry, for this deploy only. The app's
        installation token when the app is connected, else the saved read token."""
        creds = self.poller.creds
        repo = self.settings.get(REPO) or ""
        if isinstance(creds, AppCredentials) and repo:
            try:
                token = await creds.pull_token(repo.split("/")[0])
            except httpx.HTTPError as e:
                log.warning("no pull token from the app: %s", e)
                token = None
            if token:
                return {"registry": "ghcr.io", "username": "x-access-token", "password": token}
        saved = self.settings.secret(PULL_TOKEN)
        return {"registry": "ghcr.io", "username": "argus", "password": saved} if saved else None

    def views(self, companions: list[dict]) -> list[dict]:
        """For the Hosts page: each host's stacks and how their deploys went."""
        out = []
        for c in companions:
            reported = {d["path"]: d for d in ((c.get("report") or {}).get("deploys") or [])}
            stacks = []
            for s in self.stacks_for(c["name"]):
                r = reported.get(s["path"])
                if c["read_only"]:
                    status = "read_only"
                elif r is None or r.get("digest") != s["digest"]:
                    status = "pending"
                else:
                    status = {"ok": "deployed"}.get(r.get("result"), r.get("result", "pending"))
                stacks.append({"path": s["path"], "name": s["name"], "digest": s["digest"],
                               "status": status, "detail": (r or {}).get("detail", ""),
                               "at": (r or {}).get("at"),
                               "secrets": sorted(self.env(s["path"]))})
            out.append({"companion": c["name"], "stacks": stacks})
        return out
