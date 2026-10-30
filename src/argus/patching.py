"""OS patching through companions (design decisions 7 and 8).

Each host has a weekly window in its own local time. The companion says what
time it is there in every report; when the window opens, Argus decides whether
it may patch and how far, and grants a run in its signed reply. Argus decides,
so the rules hold whatever a host thinks:

- never on Sunday (backups run then), unless the owner allowed it for a host;
- one host at a time, across all hosts;
- not on a monitoring-only host;
- when the owner named a backup for the host, it must be fresh (8 days): no
  fresh backup means no patch, and Argus pages to say why.

How far a run may go follows the host's safety net:

- none, or a backup never restore-tested: security updates only, no reboot;
- a restore-tested backup, a snapshot before patching (ZFS root), or a
  backup the owner accepted untested: all updates, and a reboot when needed.

A run that reports nothing for two hours counts as failed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

SETTINGS = "patch."  # + host: the host's schedule and safety net
REQUEST = "patch.request."  # + host: "patch now" pressed
FRESH = 8 * 86400  # a backup older than this is stale
TESTED = 35 * 86400  # a restore test older than this no longer counts
WINDOW = 3 * 3600  # a run may start up to three hours after the window opens
STUCK = 2 * 3600
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULTS = {"enabled": False, "day": 1, "time": "03:00", "level": "full", "reboot": True,
            "backup": "", "restore": "", "accept_untested": False, "allow_sunday": False}

SCHEMA = """
CREATE TABLE IF NOT EXISTS patch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, requested REAL NOT NULL,
    finished REAL, result TEXT NOT NULL, level TEXT NOT NULL, reboot INTEGER NOT NULL,
    trigger TEXT NOT NULL, local_date TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS patch_runs_host ON patch_runs (host, id);
"""


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class Patching:
    def __init__(self, db: sqlite3.Connection, settings, rt, clock: Callable[[], float]) -> None:
        self.db, self.settings, self.rt, self.clock = db, settings, rt, clock
        self.db.executescript(SCHEMA)
        # Why a host's window passed without a run, for the page and the alert.
        self.skipped: dict[str, str] = {}

    def plan(self, host: str) -> dict:
        return {**DEFAULTS, **(self.settings.get(SETTINGS + host) or {})}

    def set_plan(self, host: str, plan: dict) -> None:
        self.settings.set(SETTINGS + host, {k: plan[k] for k in DEFAULTS if k in plan})

    # --- the safety net ---

    def _age(self, ref: str) -> float | None:
        """Seconds since a job's last success or a heartbeat's last beat."""
        kind, _, name = ref.partition(":")
        now = self.rt.clock()
        if kind == "job":
            for j in self.rt.job_views():
                if j["name"] == name and j["last_success"]:
                    return now - j["last_success"]
        elif kind == "heartbeat":
            for h in self.rt.heartbeat_views():
                if h["name"] == name and h["last_seen"]:
                    return now - h["last_seen"]
        return None

    def safety(self, host: str, report: dict | None) -> dict:
        """The host's safety net and what it allows."""
        p = self.plan(host)
        snapshot = (report or {}).get("snapshot") or ""
        backup_age = self._age(p["backup"]) if p["backup"] else None
        tested_age = self._age(p["restore"]) if p["restore"] else None
        fresh = backup_age is not None and backup_age < FRESH
        tested = fresh and tested_age is not None and tested_age < TESTED
        full = bool(snapshot) or tested or (fresh and p["accept_untested"])
        if snapshot:
            net = f"a {snapshot} snapshot before each run"
        elif tested:
            net = "a fresh backup with a recent restore test"
        elif fresh:
            net = "a fresh backup, never restore-tested" + (
                " (accepted)" if p["accept_untested"] else "")
        elif p["backup"]:
            net = "its backup is not fresh"
        else:
            net = "none"
        return {"net": net, "full": full, "snapshot": snapshot,
                "backup": p["backup"], "backup_age": backup_age, "fresh": fresh,
                "restore": p["restore"], "tested_age": tested_age, "tested": tested}

    # --- runs ---

    def runs(self, host: str, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id, requested, finished, result, level, reboot, trigger, local_date, summary,"
            " log FROM patch_runs WHERE host = ? ORDER BY id DESC LIMIT ?", (host, limit))]

    def running(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM patch_runs WHERE result IN ('granted', 'running')"
                               " ORDER BY id LIMIT 1").fetchone()

    def record(self, host: str, rep: dict | None) -> None:
        """The companion's account of its newest run."""
        if not rep or not rep.get("run"):
            return
        state = rep.get("state", "")
        if state not in ("running", "rebooting", "ok", "failed"):
            return
        done = state in ("ok", "failed")
        with self.db:
            self.db.execute(
                "UPDATE patch_runs SET result = ?, summary = ?, log = ?, finished = ?"
                " WHERE id = ? AND host = ? AND result IN ('granted', 'running')",
                ("running" if not done else state, str(rep.get("summary", ""))[:500],
                 str(rep.get("log", ""))[-32 * 1024:], self.clock() if done else None,
                 int(rep["run"]), host))

    def expire(self) -> None:
        """A run with no result for two hours failed: free the lock and say so."""
        with self.db:
            self.db.execute(
                "UPDATE patch_runs SET result = 'failed', finished = ?,"
                " summary = 'no result from the host for two hours' WHERE result IN"
                " ('granted', 'running') AND requested < ?", (self.clock(), self.clock() - STUCK))

    def grant(self, host: str, read_only: bool, report: dict | None) -> dict | None:
        """A run for this host now, or None. Records why a due window was skipped."""
        self.expire()
        p = self.plan(host)
        local = (report or {}).get("local") or {}
        manual = bool(self.settings.get(REQUEST + host))
        if not (p["enabled"] or manual) or read_only or not local.get("date"):
            return None
        weekday, minute = int(local.get("weekday", -1)), _minutes(local.get("time", "00:00"))
        start = _minutes(p["time"])
        in_window = weekday == p["day"] and start <= minute < start + WINDOW // 60
        if not (manual or (p["enabled"] and in_window)):
            return None
        done_today = self.db.execute(
            "SELECT 1 FROM patch_runs WHERE host = ? AND local_date = ? AND trigger = 'window'",
            (host, local["date"])).fetchone()
        if not manual and done_today:
            return None
        why = None
        if weekday == 6 and not p["allow_sunday"]:
            why = "it is Sunday there; Sundays belong to backups"
        safety = self.safety(host, report)
        if p["backup"] and not safety["fresh"] and why is None:
            age = safety["backup_age"]
            why = (f"its backup ({p['backup']}) is {int(age // 86400)} days old"
                   if age is not None else f"its backup ({p['backup']}) has never reported")
        if why:
            self.skipped[host] = why
            if manual:
                self.settings.set(REQUEST + host, None)
            return None
        if self.running() is not None:
            return None  # one host at a time: this one waits its turn, quietly
        self.skipped.pop(host, None)
        level = "full" if (p["level"] == "full" and safety["full"]) else "security"
        reboot = bool(p["reboot"] and safety["full"])
        trigger = "manual" if manual else "window"
        with self.db:
            cur = self.db.execute(
                "INSERT INTO patch_runs (host, requested, result, level, reboot, trigger,"
                " local_date) VALUES (?, ?, 'granted', ?, ?, ?, ?)",
                (host, self.clock(), level, int(reboot), trigger, local["date"]))
        self.settings.set(REQUEST + host, None)
        return {"run": cur.lastrowid, "level": level, "reboot": reboot,
                "snapshot": bool(safety["snapshot"])}

    def view(self, host: str, report: dict | None) -> dict:
        p = self.plan(host)
        safety = self.safety(host, report)
        runs = self.runs(host)
        return {"plan": p, "safety": safety, "runs": runs,
                "skipped": self.skipped.get(host), "requested": bool(
                    self.settings.get(REQUEST + host)),
                "allowed": "all updates, and reboots when needed" if safety["full"]
                else "security updates only, no reboots"}

    def problems(self, hosts: list[str]) -> list[tuple[str, str, str]]:
        """(key, title, detail) for the alerter: a failed newest run, a skipped window."""
        out = []
        for h in hosts:
            last = self.runs(h, 1)
            if last and last[0]["result"] == "failed":
                out.append((f"patch:{h}", f"patching {h} failed",
                            last[0]["summary"] or "See the Hosts page."))
            if h in self.skipped:
                out.append((f"patch-skipped:{h}", f"{h} was not patched",
                            f"Its window passed: {self.skipped[h]}."))
        return out
