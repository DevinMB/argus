"""Adopting stacks that already run on a host, in place (design decision 6).

On a host card, each running Compose project that git does not deploy yet has
"Deploy from git". Argus asks the companion to read that project's files; the
companion finds the folder and the compose files itself from Docker's labels
(Argus only names the project) and reads only plain files inside that folder.
The preview then shows what goes to the deploy repo, the secrets found in its
.env (names only), the folder it stays in, and anything that blocks it, each
with its fix. Adopt commits the compose files under sites/<host>/<project>/,
adds the stack to hosts/<host>, and seals the .env values in Argus as the
stack's secrets. From then on the companion deploys it in its own folder, so
data next to the compose file (./data) stays where it is.

What the companion collected, .env included, is kept in memory only, and only
until the admin adopts it or 15 minutes pass. It never reaches git.
"""

from __future__ import annotations

import base64
import re
import time

from argus.deploys import REPO, WRITE_TOKEN, GitHubSource

REQUEST = "adopt.request."  # + host: projects to read on the next report
DIR = "stack.dir."  # + stack path: the folder an adopted stack lives in on its host
KEEP = 15 * 60
SECRETISH = re.compile(
    r"^\s*-?\s*([A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY"
    r"|ACCESS_?KEY)[A-Za-z0-9_]*)\s*[:=]\s*[\"']?([^\"'\s#]+)", re.I | re.M)


def parse_env(text: str) -> tuple[dict[str, str], list[str]]:
    """KEY=VALUE lines of a .env; quotes stripped, comments skipped."""
    env, bad = {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key):
            bad.append(raw[:40])
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key] = value
    return env, bad


def inline_secrets(compose: str) -> list[str]:
    """Names of secret-looking values written into a compose file itself."""
    return sorted({m.group(1) for m in SECRETISH.finditer(compose)
                   if not m.group(2).startswith("$")})


class Adoption:
    def __init__(self, settings, deploys, clock=time.time) -> None:
        self.settings, self.deploys, self.clock = settings, deploys, clock
        self.collected: dict[tuple[str, str], dict] = {}  # memory only

    def projects(self, host: dict) -> list[dict]:
        """The host's running Compose projects that git does not deploy yet."""
        rep = host.get("report") or {}
        managed = {s["name"] for s in self.deploys.stacks_for(host["name"])}
        out: dict[str, dict] = {}
        for c in rep.get("containers") or []:
            p = c.get("project")
            if not p or p in managed or not c.get("working_dir"):
                continue
            entry = out.setdefault(p, {"project": p, "dir": c["working_dir"], "containers": []})
            entry["containers"].append(c["name"])
        return list(out.values())

    def request(self, host: str, project: str) -> None:
        pending = set(self.settings.get(REQUEST + host) or [])
        self.settings.set(REQUEST + host, sorted(pending | {project}))

    def to_collect(self, host: str) -> list[str]:
        return self.settings.get(REQUEST + host) or []

    def received(self, host: str, items: list[dict]) -> None:
        for it in items:
            self.collected[(host, it["project"])] = {**it, "at": self.clock()}
        if items:
            left = set(self.to_collect(host)) - {it["project"] for it in items}
            self.settings.set(REQUEST + host, sorted(left) or None)

    def _get(self, host: str, project: str) -> dict | None:
        for k in [k for k, v in self.collected.items() if self.clock() - v["at"] > KEEP]:
            del self.collected[k]
        return self.collected.get((host, project))

    def preview(self, host: dict, project: str) -> dict:
        name = host["name"]
        got = self._get(name, project)
        path = f"sites/{name}/{project}"
        if got is None:
            return {"state": "collecting" if project in self.to_collect(name) else "none",
                    "path": path}
        if got.get("error"):
            return {"state": "error", "error": got["error"], "path": path}
        files = {n: base64.b64decode(b).decode(errors="replace")
                 for n, b in (got.get("files") or {}).items()}
        env, bad = parse_env(got.get("env") or "")
        blockers = []
        repo = self.settings.get(REPO)
        src = self.deploys.source()
        if not repo or not isinstance(src, GitHubSource):
            blockers.append(("Choose a deploy repo on the GitHub page first.", "#/github"))
        elif not self.settings.has_secret(WRITE_TOKEN):
            blockers.append(("Save the deploy repo's write token on the GitHub page first.",
                             "#/github"))
        inline = sorted({n for t in files.values() for n in inline_secrets(t)})
        if inline:
            blockers.append((
                f"The compose file writes {', '.join(inline)} into itself, so it would reach git. "
                "Move each value into the project's .env and write ${NAME} in its place, then "
                "read the files again.", None))
        roots = (host.get("report") or {}).get("policy_roots") or []
        d = got["dir"]
        if roots and not any(d == r.rstrip("/") or d.startswith(r.rstrip("/") + "/")
                             for r in roots):
            blockers.append((
                f"{d} is outside the folders this host lets Argus manage "
                f"({', '.join(roots)}). On the host, add it to bind_roots in "
                "/etc/argus-companion/policy.json, then read the files again.", None))
        snap = self.deploys.snapshot
        if snap and path in snap.stacks:
            blockers.append((f"{path} already exists in the deploy repo.", None))
        return {"state": "ready", "path": path, "dir": d, "files": files,
                "secrets": sorted(env), "unreadable_env_lines": len(bad),
                "blockers": [{"text": t, "href": h} for t, h in blockers]}

    async def adopt(self, host: dict, project: str) -> str:
        """Commit the project and seal its secrets. Returns the commit."""
        pv = self.preview(host, project)
        if pv["state"] != "ready" or pv["blockers"]:
            raise ValueError("not ready: " + "; ".join(b["text"] for b in pv.get("blockers", []))
                             if pv.get("blockers") else "read the files first")
        name, path = host["name"], pv["path"]
        got = self._get(name, project)
        env, _ = parse_env(got.get("env") or "")
        snap = self.deploys.snapshot
        # Append to the host's file as written, comments and all.
        before = (snap.hosts_text.get(name) if snap else None) or ""
        hosts_text = (before.rstrip("\n") + "\n" if before.strip() else "") + path + "\n"
        files = {f"{path}/{n}": t for n, t in pv["files"].items()}
        files[f"hosts/{name}"] = hosts_text
        commit = await self.deploys.commit(
            files, f"Adopt {project} on {name}\n\nIt keeps running in {pv['dir']} on {name}; "
                   "its secrets are kept in Argus.\n")
        self.deploys.set_env(path, env)
        self.settings.set(DIR + path, pv["dir"])
        self.collected.pop((name, project), None)
        await self.deploys.refresh()
        return commit
