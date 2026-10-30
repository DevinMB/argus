"""HTTP surface.

- Open: the dashboard's reads (unless "require login to view" is on), /metrics,
  /healthz, and the two writes timers make (heartbeats and job reports).
- Behind the admin login: settings, and the actions on the Alerts page. Every
  such write needs the session's CSRF token in X-CSRF-Token.
- Companions (under /companion/, outside /api/): enrollment with a single-use
  code, then reports with the companion's own token. Replies are signed.

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
import shlex
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from argus import ghauth, github
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
from argus.companions import REPORT_EVERY, EnrollError, Signer
from argus.metrics import registry
from argus.probes import Result
from argus.runtime import Runtime
from argus.settings import Settings, load_key

STALL = 30  # seconds without a scheduler tick before /healthz fails
MAX_REPORT = 256 * 1024  # bytes in a job report
LOG_TAIL = 64 * 1024  # characters of log kept per run
# The read-only GitHub token (decision 0010). Absent: no Updates view, no tracks.
TOKEN_FILE = Path(os.environ.get("ARGUS_GITHUB_TOKEN_FILE", "/run/secrets/github_token"))
UI_DIR = Path(os.environ.get("ARGUS_UI", Path(__file__).resolve().parents[2] / "ui" / "dist"))
# The companion builds (linux-amd64, linux-arm64), put there by the image build.
COMPANION_DIR = Path(os.environ.get("ARGUS_COMPANION_DIR", "/app/companion"))
INSTALL_SH = Path(__file__).with_name("companion-install.sh")
MAX_COMPANION_REPORT = 512 * 1024  # bytes

# The UI is same-origin only: no CDNs, no external fonts, no inline scripts.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action https://github.com",
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
    rules: dict[Literal["checks", "jobs", "heartbeats", "companions"], bool] | None = None
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


class NewHost(BaseModel):
    """"Add a host": the address the host reaches Argus at, and options."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=300)
    name: str = Field("", pattern=r"^$|^[a-z0-9][a-z0-9-]{0,62}$")
    read_only: bool = False

    _url = field_validator("url")(_http_url)


class Enroll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=40)
    hostname: str = Field(max_length=253)


# What a companion reports. Unknown fields are ignored, so an older Argus still
# takes reports from a newer companion.
class Container(BaseModel):
    name: str = Field(max_length=200)
    image: str = Field("", max_length=300)
    state: str = Field("", max_length=40)  # running, exited, restarting, ...
    health: str = Field("", max_length=40)  # healthy, unhealthy, starting, or none
    status: str = Field("", max_length=200)  # Docker's words: "Up 3 hours (healthy)"
    project: str = Field("", max_length=200)  # the compose project, if any


class Disk(BaseModel):
    mount: str = Field(max_length=300)
    total: int = Field(ge=0)
    used: int = Field(ge=0)


class Updates(BaseModel):
    pending: int | None = Field(None, ge=0)
    security: int | None = Field(None, ge=0)
    manager: str = Field("", max_length=20)
    checked_at: float | None = None
    error: str = Field("", max_length=300)


class RemoteResult(BaseModel):
    name: str = Field(max_length=64)
    ok: bool
    duration: float = Field(ge=0)
    error: str = Field("", max_length=300)


class Report(BaseModel):
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    version: str = Field("", max_length=100)
    hostname: str = Field("", max_length=253)
    os: str = Field("", max_length=120)
    kernel: str = Field("", max_length=120)
    arch: str = Field("", max_length=20)
    uptime: float | None = None
    read_only: bool = False
    containers: list[Container] | None = Field(None, max_length=1000)
    docker_error: str = Field("", max_length=300)
    failed_units: list[str] | None = Field(None, max_length=200)
    disks: list[Disk] = Field([], max_length=100)
    updates: Updates | None = None
    reboot_required: bool | None = None
    checks: list[RemoteResult] = Field([], max_length=1000)


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


class GitHubSettings(BaseModel):
    """A change on the GitHub page. An empty token clears it."""

    model_config = ConfigDict(extra="forbid")
    token: str | None = Field(None, max_length=255,
                              pattern=r"^$|^(github_pat_|ghp_)[A-Za-z0-9_]{20,250}$")
    hidden: list[str] | None = Field(None, max_length=2000)


class NewApp(BaseModel):
    """"Connect GitHub": Argus's address as the browser sees it, and where the app lives."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=300)
    # GitHub app names are unique across GitHub and at most 34 characters.
    name: str = Field(min_length=3, max_length=34, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    org: str = Field("", pattern=r"^$|^[A-Za-z0-9-]{1,39}$")

    _url = field_validator("url")(_http_url)


def create_app(runtime: Runtime | None = None, start_scheduler: bool = True,
               poller: github.Poller | None = None,
               alert_client: httpx.AsyncClient | None = None,
               tls_pin: str | None = None,
               github_client: httpx.AsyncClient | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or Runtime(
            Path(os.environ.get("ARGUS_CONFIG", "/config/argus.yml")),
            os.environ.get("ARGUS_DB", "/data/argus.db"),
        )
        key = load_key(Path(os.environ.get("ARGUS_KEY", rt.db_path.parent / "argus.key")))
        settings = Settings(rt.store.db, key)
        client = alert_client or httpx.AsyncClient()
        gh_client = github_client or httpx.AsyncClient(timeout=20)
        gh = poller or github.Poller(ghauth.credentials(settings, gh_client, TOKEN_FILE, rt.clock))
        alerter = Alerter(rt, settings, client)
        auth = Auth(rt.store.db, settings, rt.clock)
        auth.setup_code()  # logs it when there is no password yet
        app.state.rt, app.state.gh, app.state.gh_client = rt, gh, gh_client
        app.state.settings, app.state.auth, app.state.alerter = settings, auth, alerter
        app.state.signer, app.state.tls_pin = Signer(settings), tls_pin
        app.state.registry = registry(rt, gh, alerter.prometheus_test_active)
        tasks = []
        if start_scheduler:
            tasks.append(asyncio.create_task(rt.run_forever()))
            tasks.append(asyncio.create_task(alerter.run_forever()))
            tasks.append(asyncio.create_task(github.run_forever(
                gh, lambda: rt.config, lambda: set(settings.get(ghauth.HIDDEN, [])))))
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await gh.aclose()
        if github_client is None:
            await gh_client.aclose()
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

    # --- GitHub: the connection ---

    def github_view(request: Request) -> dict:
        s, gh = request.app.state.settings, request.app.state.gh
        hidden = set(s.get(ghauth.HIDDEN, []))
        about = gh.creds.describe() if gh.creds else {"source": "none"}
        if about.get("app"):
            about["settings_url"] = ghauth.settings_url(about["app"])
        return {
            **about,
            "connect_error": s.get("github.connect_error"),
            "token_set": s.has_secret(ghauth.TOKEN),
            "file": TOKEN_FILE.is_file(),
            "from_config": bool(rt(request).config.github and rt(request).config.repos),
            "repos": [{**x, "hidden": x["full_name"] in hidden} for x in gh.found or []],
            "found_error": gh.found_error,
            "last_success": gh.last_success, "last_error": gh.last_error,
            "rate_remaining": gh.rate and gh.rate[0],
        }

    async def reconnect(request: Request) -> None:
        gh = request.app.state.gh
        gh.connect(ghauth.credentials(request.app.state.settings, request.app.state.gh_client,
                                      TOKEN_FILE, rt(request).clock))
        await gh.discover(force=True)

    @app.get("/api/github")
    async def github_settings(request: Request, _: Admin) -> dict:
        return github_view(request)

    @app.put("/api/github")
    async def github_change(body: GitHubSettings, request: Request, _: Admin) -> dict:
        s = request.app.state.settings
        if body.hidden is not None:
            s.set(ghauth.HIDDEN, sorted(set(body.hidden)))
        if body.token is not None:
            if s.get(ghauth.APP):
                raise HTTPException(409, "Argus's GitHub App is connected; disconnect it first")
            s.set_secret(ghauth.TOKEN, body.token or None)
            await reconnect(request)
            if body.token and request.app.state.gh.found_error:
                s.set_secret(ghauth.TOKEN, None)
                await reconnect(request)
                raise HTTPException(400, "GitHub refused that token; check it and its repo access")
        return github_view(request)

    @app.post("/api/github/refresh")
    async def github_refresh(request: Request, _: Admin) -> dict:
        await request.app.state.gh.discover(force=True)
        return github_view(request)

    @app.post("/api/github/manifest")
    async def github_manifest(body: NewApp, request: Request, _: Admin) -> dict:
        request.app.state.settings.set("github.connect_error", None)
        return ghauth.manifest(request.app.state.settings, body.url, body.name, body.org,
                               rt(request).clock)

    @app.post("/api/github/disconnect")
    async def github_disconnect(request: Request, _: Admin) -> dict:
        s = request.app.state.settings
        for secret in (ghauth.APP_KEY, ghauth.TOKEN):
            s.set_secret(secret, None)
        s.set(ghauth.APP, None)
        await reconnect(request)
        return github_view(request)

    # GitHub sends the browser here after the user confirms the new app. Outside
    # /api/: the session cookie (SameSite=strict) does not come along on a
    # redirect from github.com, so the single-use state is what proves it.
    @app.get("/github/manifest-callback")
    async def github_callback(request: Request, code: str = "", state: str = "") -> Response:
        s = request.app.state.settings
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code):
            s.set("github.connect_error", "GitHub sent no code back")
            return Response(status_code=303, headers={"Location": "/#/github"})
        try:
            app_ = await ghauth.finish(s, request.app.state.gh_client, code, state,
                                       rt(request).clock)
        except (ghauth.ConnectError, httpx.HTTPError) as e:
            s.set("github.connect_error", str(e) if isinstance(e, ghauth.ConnectError)
                  else f"could not reach GitHub ({type(e).__name__})")
            return Response(status_code=303, headers={"Location": "/#/github"})
        s.set("github.connect_error", None)
        request.app.state.gh.connect(ghauth.credentials(
            s, request.app.state.gh_client, TOKEN_FILE, rt(request).clock))
        # Next: install it on the repos to watch. GitHub then sends the browser
        # back to /#/github (the app's setup URL).
        return Response(status_code=303,
                        headers={"Location": f"{app_['html_url']}/installations/new"})

    # --- companions: the UI's side ---

    @app.get("/api/companions")
    async def companions(request: Request) -> dict:
        r = rt(request)
        views = r.companions.views()
        for v in views:
            v["checks"] = [check_view(r, c) for c in r.companion_checks(v["name"])]
        named = {c.companion for c in r.config.checks if c.companion}
        return {"companions": views, "report_every": REPORT_EVERY, "now": r.clock(),
                "tls_pin": request.app.state.tls_pin is not None,
                "unknown": sorted(named - r.companions.names())}

    @app.post("/api/companions/codes")
    async def add_host(body: NewHost, request: Request, _: Admin) -> dict:
        code, expires = rt(request).companions.new_code(body.name, body.read_only)
        url = body.url.rstrip("/")
        pin = request.app.state.tls_pin if url.startswith("https://") else None
        q = shlex.quote
        fetch = f"curl -fsS -k --pinnedpubkey {q('sha256//' + pin)}" if pin else "curl -fsS"
        args = f"--url {q(url)}" + (f" --pin {q(pin)}" if pin else "") + f" --code {code}"
        if body.read_only:
            args += " --read-only"
        return {"code": code, "expires": expires,
                "command": f"{fetch} {q(url + '/companion/install.sh')} | sudo sh -s -- {args}"}

    @app.delete("/api/companions/{cid}")
    async def remove_host(cid: str, request: Request, _: Admin) -> dict:
        if not rt(request).companions.remove(cid):
            raise HTTPException(404, "no such host")
        return {"ok": True}

    # --- companions: the hosts' side ---

    @app.get("/companion/install.sh")
    async def install_sh() -> Response:
        return Response(INSTALL_SH.read_text(), media_type="text/x-shellscript")

    @app.get("/companion/download/{build}")
    async def download(build: Literal["linux-amd64", "linux-arm64"]) -> Response:
        path = COMPANION_DIR / f"argus-companion-{build}"
        if not path.is_file():
            raise HTTPException(404, "this Argus has no companion build for " + build)
        return FileResponse(path, media_type="application/octet-stream",
                            filename=f"argus-companion-{build}")

    @app.post("/companion/enroll")
    async def enroll(body: Enroll, request: Request) -> dict:
        try:
            cid, name, token = rt(request).companions.enroll(body.code, body.hostname,
                                                             address(request))
        except EnrollError as e:
            raise HTTPException(403, str(e)) from None
        return {"id": cid, "name": name, "token": token,
                "public_key": request.app.state.signer.public, "report_every": REPORT_EVERY}

    @app.post("/companion/report")
    async def companion_report(request: Request) -> Response:
        r = rt(request)
        auth = request.headers.get("authorization", "")
        row = r.companions.by_token(auth.removeprefix("Bearer ").strip())
        if row is None:
            raise HTTPException(401, "unknown companion token; enroll this host again")
        body = await request.body()
        if len(body) > MAX_COMPANION_REPORT:
            raise HTTPException(413, "report too large")
        try:
            rep = Report.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        for c in rep.checks:
            r.record_remote(row["name"], c.name, Result(c.ok, c.duration, c.error))
        r.companions.save_report(row["id"], rep.model_dump(exclude={"nonce", "checks"}),
                                 address(request))
        reply = json.dumps({
            "name": row["name"],
            "report_every": REPORT_EVERY,
            "read_only": bool(row["read_only"]),
            "checks": [c.model_dump(include={"name", "kind", "target", "interval", "timeout",
                                             "expect_status", "verify_tls"})
                       for c in r.companion_checks(row["name"])],
        }, separators=(",", ":")).encode()
        sig = request.app.state.signer.sign(row["id"], rep.nonce, reply)
        return Response(reply, media_type="application/json",
                        headers={"X-Argus-Signature": sig})

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
        """Each polled repo's state and track, in the order they were polled."""
        r, gh = rt(request), request.app.state.gh
        if not gh.connected:
            return []
        checks = {c.name: c for c in r.config.checks}
        beats = {h["name"]: h for h in r.heartbeat_views()}
        stacks = r.config.github.stacks_repo if r.config.github else "the deploy repo"
        out = []
        for t in gh.targets:
            state = gh.repos.get(t.name)
            if state is None:
                continue
            c = checks.get(t.repo.check)
            out.append({**state, "track": github.track(
                state, c and check_view(r, c), beats, r.clock(), stacks, t.repo.deploy)})
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
            "enabled": gh.connected,
            "owners": sorted({t.owner for t in gh.targets}),
            "last_success": gh.last_success,
            "last_error": gh.last_error or gh.found_error,
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
