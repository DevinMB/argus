"""HTTP surface.

- Open: the dashboard's reads (unless "require login to view" is on), /metrics,
  /healthz, and the two writes timers make (heartbeats and job reports).
- Behind the admin login: settings, and the actions on the Alerts page. Every
  such write needs the session's CSRF token in X-CSRF-Token.

Handlers are async on purpose: they then run on the event loop thread, the same
one the scheduler writes from, so SQLite needs no locking.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from argus import github
from argus.alerts import (
    ALERTMANAGER_EXAMPLE,
    TOKEN,
    WATCH_URL,
    Alerter,
    SendError,
    prometheus_rules,
    recent_chats,
)
from argus.auth import COOKIE, SESSION_DAYS, Auth, AuthError, Session
from argus.metrics import registry
from argus.runtime import Runtime
from argus.settings import Settings, load_key

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


class Password(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)


class Setup(Password):
    code: str = Field(max_length=32)


class NewPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


def _http_url(v: str | None) -> str | None:
    if v and (urlsplit(v).scheme not in ("http", "https") or not urlsplit(v).hostname):
        raise ValueError("must be an http(s) URL")
    return v


class AlertSettings(BaseModel):
    """A change on the Alerts page. Absent fields stay as they are; an empty secret
    clears it. Secrets are write-only."""

    model_config = ConfigDict(extra="forbid")
    telegram_token: str | None = Field(None, max_length=200, pattern=r"^$|^\d{3,20}:[\w-]{20,100}$")
    chat_id: str | None = Field(None, pattern=r"^$|^-?\d{1,20}$|^@[A-Za-z0-9_]{5,32}$")
    rules: dict[Literal["checks", "jobs", "heartbeats"], bool] | None = None
    repeat_minutes: int | None = Field(None, ge=0, le=7 * 24 * 60)
    link: str | None = Field(None, max_length=300)
    watch_url: str | None = Field(None, max_length=500)

    _urls = field_validator("link", "watch_url")(_http_url)


class Switch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: Literal["argus", "prometheus"]


class Access(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view_requires_login: bool


def session_of(request: Request) -> Session | None:
    return request.app.state.auth.session(request.cookies.get(COOKIE))


def admin(request: Request) -> Session:
    """The admin's session, or 401. (The view_gate middleware has already checked
    the CSRF token of every write.)"""
    s = session_of(request)
    if s is None:
        raise HTTPException(401, "sign in first")
    return s


# At module level: annotations are strings here, resolved in module globals.
Admin = Annotated[Session, Depends(admin)]

# Reachable without a session even when viewing needs one.
OPEN_API = {"/api/session", "/api/setup", "/api/login"}


def _poller() -> github.Poller | None:
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return github.Poller(token) if token else None


def create_app(runtime: Runtime | None = None, start_scheduler: bool = True,
               poller: github.Poller | None = None,
               alert_client: httpx.AsyncClient | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or Runtime(
            Path(os.environ.get("ARGUS_CONFIG", "/config/argus.yml")),
            os.environ.get("ARGUS_DB", "/data/argus.db"),
        )
        gh = poller or (_poller() if start_scheduler else None)
        key = load_key(Path(os.environ.get("ARGUS_KEY", rt.db_path.parent / "argus.key")))
        settings = Settings(rt.store.db, key)
        client = alert_client or httpx.AsyncClient()
        alerter = Alerter(rt, settings, client)
        auth = Auth(rt.store.db, settings, rt.clock)
        auth.setup_code()  # logs it when there is no password yet
        app.state.rt, app.state.gh = rt, gh
        app.state.settings, app.state.auth, app.state.alerter = settings, auth, alerter
        app.state.registry = registry(rt, gh, alerter.prometheus_test_active)
        tasks = []
        if start_scheduler:
            tasks.append(asyncio.create_task(rt.run_forever()))
            tasks.append(asyncio.create_task(alerter.run_forever()))
            if gh:
                tasks.append(asyncio.create_task(github.run_forever(gh, lambda: rt.config)))
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if gh:
            await gh.aclose()
        if alert_client is None:
            await client.aclose()
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

    def address(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def deny(status: int, detail: str) -> Response:
        # This middleware wraps security_headers, so it adds them itself.
        return Response(json.dumps({"detail": detail}), status,
                        media_type="application/json", headers=SECURITY_HEADERS)

    @app.middleware("http")
    async def view_gate(request: Request, call_next):
        """Every write under /api/ needs the admin's session and CSRF token, and with
        "require login to view" on, so do the reads. /metrics, /healthz and the
        timers' writes stay open."""
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_API:
            s = session_of(request)
            write = request.method not in ("GET", "HEAD")
            # Writes are checked here, before the body is even parsed, so a
            # stranger learns nothing from validation errors.
            if s is None and (write or request.app.state.settings.get(
                    "access.view_requires_login", False)):
                return deny(401, "sign in first")
            if write and not hmac.compare_digest(
                    request.headers.get("x-csrf-token", "").encode(), s.csrf.encode()):
                return deny(403, "missing or wrong CSRF token; reload the page")
        return await call_next(request)

    def signed_in(request: Request, response: Response, token: str) -> dict:
        response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/",
                            httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
        return session_view(request, request.app.state.auth.session(token))

    def session_view(request: Request, s: Session | None) -> dict:
        auth = request.app.state.auth
        return {"setup_needed": auth.needs_setup(), "signed_in": s is not None,
                "csrf": s.csrf if s else None,
                "view_requires_login": request.app.state.settings.get(
                    "access.view_requires_login", False)}

    def auth_call(fn, *args):
        try:
            return fn(*args)
        except AuthError as e:
            raise HTTPException(e.status, str(e)) from None

    @app.get("/api/session")
    async def get_session(request: Request) -> dict:
        request.app.state.auth.setup_code()  # re-logs a code after reset-login
        return session_view(request, session_of(request))

    @app.post("/api/setup")
    async def setup(body: Setup, request: Request, response: Response) -> dict:
        token = auth_call(request.app.state.auth.setup, body.code, body.password,
                          address(request))
        return signed_in(request, response, token)

    @app.post("/api/login")
    async def login(body: Password, request: Request, response: Response) -> dict:
        token = auth_call(request.app.state.auth.login, body.password, address(request))
        return signed_in(request, response, token)

    @app.post("/api/logout")
    async def logout(request: Request, response: Response,
                     _: Admin) -> dict:
        request.app.state.auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return session_view(request, None)

    @app.post("/api/password")
    async def password(body: NewPassword, request: Request, response: Response,
                       _: Admin) -> dict:
        token = auth_call(request.app.state.auth.change_password,
                          request.cookies.get(COOKIE), body.current, body.new, address(request))
        return signed_in(request, response, token)

    @app.put("/api/access")
    async def access(body: Access, request: Request, _: Admin) -> dict:
        request.app.state.settings.set("access.view_requires_login", body.view_requires_login)
        return session_view(request, session_of(request))

    @app.get("/api/alerts")
    async def alerts(request: Request, _: Admin) -> dict:
        return request.app.state.alerter.view()

    @app.put("/api/alerts")
    async def alert_settings(body: AlertSettings, request: Request,
                             _: Admin) -> dict:
        s, alerter = request.app.state.settings, request.app.state.alerter
        if body.telegram_token is not None:
            s.set_secret(TOKEN, body.telegram_token)
        if body.watch_url is not None:
            s.set_secret(WATCH_URL, body.watch_url)
            s.set("watch.state", None)
        if body.telegram_token is not None or body.chat_id is not None:
            s.set("alerts.test_ok_at", None)  # a new destination needs a new test
            s.set("alerts.delivery", None)
        for field, key in (("chat_id", "alerts.telegram.chat_id"), ("rules", "alerts.rules"),
                           ("repeat_minutes", "alerts.repeat_minutes"), ("link", "alerts.link")):
            value = getattr(body, field)
            if value is not None:
                s.set(key, value)
        return alerter.view()

    @app.post("/api/alerts/test")
    async def alert_test(request: Request, _: Admin) -> dict:
        error = await request.app.state.alerter.test()
        return {"ok": error is None, "error": error}

    @app.post("/api/alerts/telegram-chats")
    async def telegram_chats(request: Request, _: Admin) -> dict:
        token = request.app.state.settings.secret(TOKEN)
        if not token:
            raise HTTPException(409, "save the bot token first")
        try:
            chats = await recent_chats(token, request.app.state.alerter.client)
        except SendError as e:
            raise HTTPException(502, str(e)) from None
        return {"chats": chats}

    @app.post("/api/alerts/prometheus-test")
    async def prometheus_test(request: Request, _: Admin) -> dict:
        request.app.state.alerter.prometheus_test()
        return request.app.state.alerter.view()

    @app.post("/api/alerts/sender")
    async def switch(body: Switch, request: Request, _: Admin) -> dict:
        why = request.app.state.alerter.switch(body.sender)
        if why:
            raise HTTPException(409, why)
        return request.app.state.alerter.view()

    @app.get("/api/alerts/prometheus-rules.yml")
    async def rules_file(request: Request) -> Response:
        return Response(prometheus_rules(request.app.state.settings.get("alerts.link", "")),
                        media_type="application/yaml", headers={
                            "Content-Disposition": 'attachment; filename="argus-rules.yml"'})

    @app.get("/api/alerts/alertmanager-example.yml")
    async def alertmanager_file() -> Response:
        return Response(ALERTMANAGER_EXAMPLE, media_type="application/yaml", headers={
            "Content-Disposition": 'attachment; filename="alertmanager-example.yml"'})

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
            "alerts": request.app.state.alerter.summary(),
        }

    def check_view(r: Runtime, c) -> dict:
        s = r.store.get(c.name)
        return {**c.model_dump(), **{k: v for k, v in vars(s).items() if k != "name"}}

    @app.get("/api/checks")
    async def checks(request: Request) -> list[dict]:
        r = rt(request)
        return [check_view(r, c) for c in r.config.checks]

    def repo_views(request: Request) -> list[dict]:
        """Each configured repo's last polled state and track, in config order."""
        r, gh = rt(request), request.app.state.gh
        if gh is None or r.config.github is None:
            return []
        checks = {c.name: c for c in r.config.checks}
        beats = {h["name"]: h for h in r.heartbeat_views()}
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
        return rt(request).heartbeat_views()

    @app.get("/api/jobs")
    async def jobs(request: Request) -> list[dict]:
        return rt(request).job_views()

    @app.get("/api/jobs/{name}")
    async def job(name: str, request: Request) -> dict:
        r = rt(request)
        for view in r.job_views():
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
