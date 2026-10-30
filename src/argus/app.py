"""HTTP surface. Read-only apart from heartbeats and job reports: no login, no
config editing, no actions. Everything that pages is a Prometheus rule over
/metrics.

Handlers are async on purpose: they then run on the event loop thread, the same
one the scheduler writes from, so SQLite needs no locking.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from argus import github
from argus.metrics import registry
from argus.runtime import Runtime

STALL = 30  # seconds without a scheduler tick before /healthz fails
MAX_REPORT = 256 * 1024  # bytes in a job report
LOG_TAIL = 64 * 1024  # characters of log kept per run
# The read-only GitHub token (decision 0010). Absent: no Updates view, no tracks.
TOKEN_FILE = Path(os.environ.get("ARGUS_GITHUB_TOKEN_FILE", "/run/secrets/github_token"))
UI_DIR = Path(os.environ.get("ARGUS_UI", Path(__file__).resolve().parents[2] / "ui" / "dist"))

# The UI is same-origin only: no CDNs, no external fonts, no inline scripts.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class RunItem(BaseModel):
    """One thing a run covered (a guest, a host, a playbook) and how it went."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    result: Literal["ok", "failed", "skipped", "pending"]
    detail: str = Field("", max_length=300)


class JobRun(BaseModel):
    """What a scheduled run reports when it ends (decision 0017)."""

    model_config = ConfigDict(extra="forbid")

    result: Literal["ok", "failed"]
    started: float = Field(gt=0)
    finished: float = Field(gt=0)
    summary: str = Field("", max_length=500)
    items: list[RunItem] = Field([], max_length=200)
    # The tail is what matters (where it failed); longer logs are cut from the front.
    log: str = ""

    @model_validator(mode="after")
    def _order(self) -> JobRun:
        if self.finished < self.started:
            raise ValueError("finished is before started")
        self.log = self.log[-LOG_TAIL:]
        return self


def _poller() -> github.Poller | None:
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return github.Poller(token) if token else None


def create_app(runtime: Runtime | None = None, start_scheduler: bool = True,
               poller: github.Poller | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or Runtime(
            Path(os.environ.get("ARGUS_CONFIG", "/config/argus.yml")),
            os.environ.get("ARGUS_DB", "/data/argus.db"),
        )
        gh = poller or (_poller() if start_scheduler else None)
        app.state.rt, app.state.gh = rt, gh
        app.state.registry = registry(rt, gh)
        tasks = []
        if start_scheduler:
            tasks.append(asyncio.create_task(rt.run_forever()))
            if gh:
                tasks.append(asyncio.create_task(github.run_forever(gh, lambda: rt.config)))
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if gh:
            await gh.aclose()
        await rt.aclose()

    app = FastAPI(title="Argus", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def rt(request: Request) -> Runtime:
        return request.app.state.rt

    @app.get("/healthz")
    async def healthz(request: Request) -> Response:
        r = rt(request)
        ok = r.clock() - r.last_tick < STALL
        return Response("ok\n" if ok else "scheduler stalled\n", 200 if ok else 503,
                        media_type="text/plain")

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(request.app.state.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/info")
    async def info(request: Request) -> dict:
        r = rt(request)
        return {
            "version": os.environ.get("ARGUS_VERSION", "dev"),
            "commit": os.environ.get("ARGUS_COMMIT", "unknown"),
            "config": r.config_hash,
            "config_reload_ok": r.reload_ok,
            "last_tick": r.last_tick,
            "now": r.clock(),
            "groups": r.config.groups,
            "sites": r.config.sites,
            "links": [link.model_dump() for link in r.config.links],
            "jobs_intro": r.config.jobs_intro,
        }

    def check_view(r: Runtime, c) -> dict:
        s = r.store.get(c.name)
        return {**c.model_dump(), **{k: v for k, v in vars(s).items() if k != "name"}}

    @app.get("/api/checks")
    async def checks(request: Request) -> list[dict]:
        r = rt(request)
        return [check_view(r, c) for c in r.config.checks]

    def heartbeat_views(r: Runtime) -> list[dict]:
        beats, now = r.store.heartbeats(), r.clock()
        out = []
        for h in r.config.heartbeats:
            b = beats.get(h.name)
            age = now - b["last_seen"] if b else None
            status = "never" if b is None else ("late" if age > h.max_age else "ok")
            out.append({**h.model_dump(), "status": status, "age": age,
                        "last_seen": b and b["last_seen"], "source": b and b["source"],
                        "version": b and b["version"], "version_at": b and b["version_at"]})
        return out

    def job_views(r: Runtime) -> list[dict]:
        last, now = r.store.last_runs(), r.clock()
        out = []
        for j in r.config.jobs:
            run = last.get(j.name)
            if run is None:
                status = "never"
            elif run["result"] == "failed":
                status = "failed"
            else:
                status = "late" if now - run["finished"] > j.max_age else "ok"
            out.append({**j.model_dump(), "status": status, "last": run,
                        "last_success": run and run["last_success"]})
        return out

    def repo_views(request: Request) -> list[dict]:
        """Each configured repo's last polled state and track, in config order."""
        r, gh = rt(request), request.app.state.gh
        if gh is None or r.config.github is None:
            return []
        checks = {c.name: c for c in r.config.checks}
        beats = {h["name"]: h for h in heartbeat_views(r)}
        out = []
        for repo in r.config.repos:
            state = gh.repos.get(repo.name)
            if state is None:
                continue
            c = checks.get(repo.check)
            out.append({**state, "track": github.track(
                state, c and check_view(r, c), beats, r.clock(), r.config.github.stacks_repo,
                repo.deploy)})
        return out

    @app.get("/api/checks/{name}")
    async def check(name: str, request: Request) -> dict:
        r = rt(request)
        for c in r.config.checks:
            if c.name == name:
                repo = next((x for x in repo_views(request) if x["check"] == name), None)
                return {**check_view(r, c), "transitions": r.store.transitions(name),
                        "repo": repo and repo["name"], "track": repo and repo["track"]}
        raise HTTPException(404, "no such check")

    @app.get("/api/heartbeats")
    async def heartbeats(request: Request) -> list[dict]:
        return heartbeat_views(rt(request))

    @app.get("/api/jobs")
    async def jobs(request: Request) -> list[dict]:
        return job_views(rt(request))

    @app.get("/api/jobs/{name}")
    async def job(name: str, request: Request) -> dict:
        r = rt(request)
        for view in job_views(r):
            if view["name"] == name:
                return {**view, "runs": r.store.runs(name)}
        raise HTTPException(404, "no such job")

    @app.get("/api/updates")
    async def updates(request: Request) -> dict:
        r, gh = rt(request), request.app.state.gh
        return {
            "enabled": gh is not None and r.config.github is not None,
            "owner": r.config.github and r.config.github.owner,
            "last_success": gh and gh.last_success,
            "last_error": gh.last_error if gh else "",
            "now": r.clock(),
            "repos": repo_views(request),
        }

    # The one write: a timer saying "I ran". Only configured names are accepted.
    # A timer that deploys an app adds ?version=<commit> for what runs now.
    @app.post("/heartbeat/{name}", status_code=204)
    async def heartbeat(name: str, request: Request, version: str | None = None) -> Response:
        source = request.client.host if request.client else "unknown"
        if version is not None and not re.fullmatch(r"[0-9a-f]{7,40}", version):
            raise HTTPException(422, "version must be a commit sha")
        if not rt(request).beat(name, source, version):
            raise HTTPException(404, "no such heartbeat")
        return Response(status_code=204)

    # The other write: a scheduled run reporting how it went (decision 0017).
    # Only configured names are accepted, and the body is size-capped.
    @app.post("/job/{name}", status_code=204)
    async def report(name: str, request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_REPORT:
            raise HTTPException(413, "report too large")
        try:
            run = JobRun.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        source = request.client.host if request.client else "unknown"
        if not rt(request).report(name, run.model_dump(), source):
            raise HTTPException(404, "no such job")
        return Response(status_code=204)

    # The read-only UI (ui/, built into the image). Mounted last so the API
    # routes above win; absent in development until `npm run build`.
    if UI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

    # The page shell names the build's hashed assets, so a browser must ask for
    # it again each time or it keeps showing the previous UI after a deploy.
    @app.middleware("http")
    async def fresh_shell(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app
