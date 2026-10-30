"""The app pipeline as GitHub sees it, for the Updates view and each tile's
track (decision 0010). Polled every `github.poll` seconds with the read-only
token and held in memory: a restart only waits for the next poll.

Every step of a track comes from a real event: a PR, a workflow run, a commit
in the stacks repo, a stack-sync heartbeat, the check itself. A step whose
pipeline does not exist yet says "not wired", never green.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from argus.config import GitHub, Repo

log = logging.getLogger("argus.github")

API = "https://api.github.com"
GATE = "github-actions[bot]"  # the Dependabot merge gate merges as this
DEPENDABOT = "dependabot[bot]"
WAITS = "waits-for-devin"  # the gate's label for a major update
MERGED_WINDOW = 14 * 86400
BUMP_GRACE = 1800  # seconds after a green build before a missing bump is a failure
PR_REF = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")
BAD = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


def ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:300]


class Poller:
    def __init__(self, token: str, clock: Callable[[], float] = time.time,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.clock = clock
        self.client = httpx.AsyncClient(
            base_url=API, timeout=20, transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "argus"},
        )
        self.repos: dict[str, dict] = {}
        # (repo, PR) -> who merged it. The PR list leaves merged_by out; a merged
        # PR never changes, so each is fetched once.
        self.merged_by: dict[tuple[str, int], str | None] = {}
        self.last_success: float | None = None
        self.last_error = ""

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, missing: tuple[int, ...] = (404,), **params) -> Any:
        """JSON for a GET; None when the status is in `missing`."""
        r = await self.client.get(url, params=params)
        if r.status_code in missing:
            return None
        r.raise_for_status()
        return r.json()

    async def poll(self, gh: GitHub, repos: tuple[Repo, ...]) -> None:
        """Refresh every repo. On any error the previous results stay."""
        try:
            hosts = await self._hosts(gh) if any(r.stack for r in repos) else {}
            states = await asyncio.gather(*(self._repo(gh, r, hosts) for r in repos))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            self.last_error = _err(e)
            log.warning("github poll failed, keeping the last results: %s", self.last_error)
            return
        self.repos = {s["name"]: s for s in states}
        self.last_success, self.last_error = self.clock(), ""

    async def _file(self, gh: GitHub, path: str) -> str | None:
        f = await self._get(f"/repos/{gh.owner}/{gh.stacks_repo}/contents/{path}")
        return base64.b64decode(f["content"]).decode() if f else None

    async def _hosts(self, gh: GitHub) -> dict[str, str]:
        """Stack path -> the host whose hosts/<hostname> lists it."""
        listing = await self._get(f"/repos/{gh.owner}/{gh.stacks_repo}/contents/hosts") or []
        out = {}
        for f in listing:
            if f["type"] != "file":
                continue
            for line in (await self._file(gh, f["path"]) or "").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    out[line] = f["name"]
        return out

    async def _repo(self, gh: GitHub, repo: Repo, hosts: dict[str, str]) -> dict:
        base = f"/repos/{gh.owner}/{repo.name}"
        open_prs = await self._get(f"{base}/pulls", state="open", per_page=50) or []
        closed = await self._get(f"{base}/pulls", state="closed", sort="updated",
                                 direction="desc", per_page=30) or []
        runs = await self._get(f"{base}/actions/workflows/ci.yml/runs", branch="main", per_page=10)
        run = next((x for x in (runs or {}).get("workflow_runs", [])
                    if x["event"] in ("push", "workflow_dispatch")), None)
        # Dependabot's version updates run as "dynamic" workflow runs.
        dyn = await self._get(f"{base}/actions/runs", event="dynamic", per_page=30)
        # 403 too: Dependabot alerts off, or not readable with this token.
        alerts = await self._get(f"{base}/dependabot/alerts", missing=(403, 404),
                                 state="open", per_page=100)
        now = self.clock()
        merged = [p for p in closed if p["merged_at"] and now - ts(p["merged_at"]) < MERGED_WINDOW]
        for p in merged:
            await self._merger(base, repo.name, p["number"])
        main = await self._main(base, repo.name, run) if run else None
        return {
            "name": repo.name,
            "url": f"https://github.com/{gh.owner}/{repo.name}",
            "check": repo.check or None,
            "main": main,
            "updates": [await self._update(base, p) for p in open_prs
                        if p["user"]["login"] == DEPENDABOT],
            "merged": [
                {"number": p["number"], "title": p["title"], "url": p["html_url"],
                 "merged_at": ts(p["merged_at"]),
                 "by": self.merged_by.get((repo.name, p["number"])),
                 "dependabot": p["user"]["login"] == DEPENDABOT}
                for p in merged
            ],
            "alerts": None if alerts is None else [
                {"severity": a["security_vulnerability"]["severity"],
                 "package": a["security_vulnerability"]["package"]["name"],
                 "summary": a["security_advisory"]["summary"],
                 "created_at": ts(a["created_at"]), "url": a["html_url"]}
                for a in alerts
            ],
            "stack": await self._stack(gh, repo, hosts) if repo.stack else None,
            "dependabot": dependabot_runs((dyn or {}).get("workflow_runs", [])),
        }

    async def _merger(self, base: str, repo: str, number: int) -> str | None:
        key = (repo, number)
        if key not in self.merged_by:
            pr = await self._get(f"{base}/pulls/{number}")
            self.merged_by[key] = ((pr or {}).get("merged_by") or {}).get("login")
        return self.merged_by[key]

    async def _main(self, base: str, repo: str, run: dict) -> dict:
        jobs = (await self._get(f"{base}/actions/runs/{run['id']}/jobs") or {}).get("jobs", [])
        steps = [(j["name"], s) for j in jobs for s in j.get("steps", [])]
        trivy = next((s["conclusion"] for _, s in steps if s["name"].startswith("Trivy")), None)
        failed = next((f"{j}: {s['name']}" for j, s in steps if s["conclusion"] in BAD), None)
        message = (run.get("head_commit") or {}).get("message", "")
        m = PR_REF.search(message)
        pr = int(m.group(1) or m.group(2)) if m else None
        if run["status"] != "completed":
            status = "running"
        else:
            status = "success" if run["conclusion"] == "success" else "failure"
        return {
            "sha": run["head_sha"][:7], "status": status, "url": run["html_url"],
            # Without the "(#12)" a squash merge appends; the track shows the PR itself.
            "title": re.sub(r"\s*\(#\d+\)$", "", message.splitlines()[0]) if message else "",
            "pr": pr, "merged_by": await self._merger(base, repo, pr) if pr else None,
            "started_at": ts(run.get("run_started_at") or run["created_at"]),
            "updated_at": ts(run["updated_at"]),
            "trivy": trivy, "failed_step": failed,
        }

    async def _update(self, base: str, pr: dict) -> dict:
        runs = (await self._get(f"{base}/actions/runs", head_sha=pr["head"]["sha"], per_page=20)
                or {}).get("workflow_runs", [])
        if not runs:
            ci = "none"
        elif any(r["status"] != "completed" for r in runs):
            ci = "running"
        elif any(r["conclusion"] in BAD for r in runs):
            ci = "failure"
        else:
            ci = "success"
        return {"number": pr["number"], "title": pr["title"], "url": pr["html_url"],
                "created_at": ts(pr["created_at"]), "ci": ci,
                "waits_for_devin": any(label["name"] == WAITS for label in pr["labels"])}

    async def _stack(self, gh: GitHub, repo: Repo, hosts: dict[str, str]) -> dict:
        path = f"{repo.stack}/compose.yml"
        text = await self._file(gh, path) or ""
        # <repo> or <repo>-<component>; -beta follows another branch, so it is not main's.
        tag = re.compile(rf"ghcr\.io/{re.escape(gh.owner.lower())}/{re.escape(repo.name.lower())}"
                         r"(-[a-z0-9-]+)?:sha-([0-9a-f]{7,40})")
        shas = sorted({m.group(2)[:7] for m in tag.finditer(text) if m.group(1) != "-beta"})
        commits = await self._get(f"/repos/{gh.owner}/{gh.stacks_repo}/commits",
                                  path=path, per_page=1) or []
        last = commits[0] if commits else None
        return {
            "path": repo.stack, "host": hosts.get(repo.stack), "shas": shas,
            "changed_at": last and ts(last["commit"]["committer"]["date"]),
            # Any change to the stack: this app's tag, or another image in it (cloudflared).
            "change": last and (last["commit"].get("message") or "").partition("\n")[0],
            "url": f"https://github.com/{gh.owner}/{gh.stacks_repo}/blob/main/{path}",
        }


def dependabot_runs(runs: list[dict]) -> dict | None:
    """When Dependabot last looked for new versions, and whether that look failed.
    One look is a batch of runs, one per ecosystem, started within the hour."""
    ups = [r for r in runs if r.get("path", "").startswith("dynamic/dependabot/dependabot-updates")]
    if not ups:
        return None
    newest = max(ts(r["created_at"]) for r in ups)
    batch = [r for r in ups if newest - ts(r["created_at"]) < 3600]
    bad = next((r for r in batch if r["conclusion"] in BAD), None)
    return {"last_run": newest, "failed": bad and bad["name"], "url": bad and bad["html_url"]}


async def run_forever(poller: Poller, config: Callable[[], Any]) -> None:
    """Poll on the configured interval; the config is re-read each round."""
    while True:
        cfg = config()
        if cfg.github:
            try:
                await poller.poll(cfg.github, cfg.repos)
            except Exception:
                log.exception("github poll crashed")
        await asyncio.sleep(cfg.github.poll if cfg.github else 60)


TITLES = {"updates": "Update PRs", "merged": "Merged to main", "built": "Built and scanned",
          "bumped": "Tag bumped", "deployed": "Deployed", "healthy": "Healthy"}


def _who(main: dict) -> str:
    if main["pr"] is None:
        return "pushed to main"
    by = "the gate" if main["merged_by"] == GATE else main["merged_by"]
    return f"#{main['pr']}, merged by {by}" if by else f"#{main['pr']}"


def track(state: dict, check: dict | None, beats: dict[str, dict], now: float,
          stacks_repo: str = "homelab-stacks", deploy: str = "") -> list[dict]:
    """The steps a change takes from update PR to healthy, for the newest change on main.

    Statuses: done, running, waiting (for Devin), failed, pending (an earlier
    step has not finished), not_wired (that part of the pipeline does not exist yet).
    A repo deployed by its own timer (`deploy`, a heartbeat) has no tag bump; one
    deployed by nothing (a library shipped inside another app) ends at the build.
    """
    steps = []

    def add(key, status, detail, url=None, at=None):
        steps.append({"key": key, "title": TITLES[key], "status": status, "detail": detail,
                      "url": url, "at": at})

    ups = state["updates"]
    dep = state.get("dependabot")
    n = len(state.get("alerts") or [])
    alerts = f". {n} open security alert{'s' if n > 1 else ''}" if n else ""
    failing = [p for p in ups if p["ci"] == "failure" and not p["waits_for_devin"]]
    waiting = [p for p in ups if p["waits_for_devin"]]
    more = lambda n: f" (+{n} more)" if n > 1 else ""  # noqa: E731
    if failing:
        add("updates", "failed", f"{failing[0]['title']}: CI failed{more(len(failing))}",
            failing[0]["url"])
    elif waiting:
        add("updates", "waiting", f"{waiting[0]['title']} waits for you{more(len(waiting))}",
            waiting[0]["url"])
    elif ups:
        add("updates", "running", f"{len(ups)} in CI; the gate merges patch and minor on green",
            ups[0]["url"])
    elif dep and dep["failed"]:
        add("updates", "failed", f"Dependabot's last look failed: {dep['failed']}", dep["url"],
            dep["last_run"])
    else:
        looked = "Dependabot last looked for new versions" if dep else "No Dependabot runs seen"
        add("updates", "done", f"No open update PRs. {looked}{alerts}", at=dep and dep["last_run"])

    main = state["main"]
    built = main["status"] if main else "none"
    if main is None:
        add("merged", "not_wired", "No ci.yml runs on main")
    else:
        pr_url = state["url"] + (f"/pull/{main['pr']}" if main["pr"] else "")
        add("merged", "done", f"{main['title']} ({_who(main)})", pr_url, main["started_at"])
        if built == "running":
            add("built", "running", f"Building {main['sha']}", main["url"], main["started_at"])
        elif built == "success":
            scan = {"success": "Trivy clean", None: "no Trivy step"}.get(
                main["trivy"], f"Trivy {main['trivy']}")
            add("built", "done", f"sha-{main['sha']}, {scan}", main["url"], main["updated_at"])
        else:
            add("built", "failed", f"Failed at {main['failed_step'] or 'an unknown step'}",
                main["url"], main["updated_at"])

    stack = state["stack"]
    deployed = False
    if stack is None and deploy:
        hb = beats.get(deploy)
        running = (hb or {}).get("version") or ""
        if built != "success":
            add("deployed", "pending", "Waits for a green build on main")
        elif hb is None:
            add("deployed", "not_wired", f"No {deploy} heartbeat")
        elif running[:7] == main["sha"]:
            deployed = True
            add("deployed", "done", f"sha-{main['sha']} is running, deployed by {deploy}",
                at=hb["version_at"])
        elif hb["status"] == "late":
            add("deployed", "failed", f"{deploy} is late; see journalctl -u {deploy}")
        elif running:
            add("deployed", "running", f"{deploy} still runs sha-{running[:7]}; "
                f"journalctl -u {deploy} says when sha-{main['sha']} goes out")
        else:
            add("deployed", "not_wired", f"{deploy} does not report which version it runs")
    elif stack is None:
        pass
    else:
        bumped = built == "success" and main["sha"] in stack["shas"]
        if built != "success":
            add("bumped", "pending", "Waits for a green build on main")
        elif bumped:
            # No time: the stack's last commit may be another image's bump.
            add("bumped", "done", f"sha-{main['sha']} in {stacks_repo}", stack["url"])
        elif now - main["updated_at"] < BUMP_GRACE:
            add("bumped", "running", "stack-bump runs every 5 minutes", stack["url"])
        else:
            tags = ", ".join("sha-" + s for s in stack["shas"]) or "no tag"
            add("bumped", "failed", f"Still {tags} 30 minutes after the build; "
                "see journalctl -u stack-bump", stack["url"])

        host = stack["host"]
        hb = beats.get(f"stack-sync-{host}") if host else None
        if not bumped:
            add("deployed", "pending", "Waits for the tag bump")
        elif host is None:
            add("deployed", "failed", f"No hosts/ file in {stacks_repo} lists it")
        elif hb is None:
            add("deployed", "not_wired", f"No stack-sync-{host} heartbeat")
        elif hb["last_seen"] and hb["last_seen"] >= (stack["changed_at"] or 0):
            deployed = True
            last = f". Last stack change: {stack['change']}" if stack.get("change") else ""
            add("deployed", "done", f"On {host} by stack-sync{last}", stack["url"],
                stack["changed_at"])
        elif hb["status"] == "late":
            add("deployed", "failed", f"stack-sync-{host} is late; see journalctl -u stack-sync")
        else:
            add("deployed", "running", f"stack-sync on {host} runs every 5 minutes")

    if check is None and stack is None and not deploy:
        pass
    elif check is None:
        add("healthy", "not_wired", "No check linked in argus.yml")
    elif (stack is not None or deploy) and not deployed:
        add("healthy", "pending", f"{check['name']} is {check['status']} on the previous deploy")
    elif check["status"] == "up":
        add("healthy", "done", f"{check['name']} is up, with no failure since",
            at=check.get("last_change"))
    elif check["status"] == "unknown":
        add("healthy", "running", f"{check['name']} has no result yet")
    else:
        why = f": {check['last_error']}" if check.get("last_error") else ""
        add("healthy", "failed", f"{check['name']} is {check['status']}{why}")
    return steps
