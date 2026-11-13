"""OS patching through companions (design decisions 7 and 12).

Each host has a weekly window in its own local time, on any day the owner
picks. The companion says what time it is there in every report; when the
window opens, Argus decides whether it may patch, and grants a run in its
signed reply. Argus decides, so the rules hold whatever a host thinks:

- no safety net, no patching: the host needs a backup that reports to Argus
  and is newer than the owner's limit for it (default 8 days), or a ZFS root
  it snapshots just before. A stale backup skips the window and pages. A
  guest on Proxmox needs nothing wired: the companion on its Proxmox server
  reports each guest's newest vzdump backup (pve:<server>/<vmid>), and Argus
  suggests the one whose guest has the host's name;
- one host at a time, across all hosts;
- not on a monitoring-only host.

With a safety net, a run goes as far as the owner chose: security updates or
all of them, and a reboot when one is needed if allowed. A restore test is
optional: shown on the card, encouraged, never required.

A run that reports nothing for two hours counts as failed.

Urgent security fixes do not wait for the weekly window (decision 22): when a
pending update closes a CVE that is being exploited or is rated critical
(argus.urgent), the host installs just those packages at its usual hour that
same night, any day, under every rule above. It reboots only if its plan
allows; otherwise the fix is installed and Argus says a reboot is waiting.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

SETTINGS = "patch."  # + host: the host's schedule and safety net
REQUEST = "patch.request."  # + host: "patch now" pressed
NOTICES = "patch.notices"  # good news to send once: an urgent fix went in
URGENT_WHY = "patch.urgent_why"  # run id -> what it fixes, until the run ends
TESTED = 35 * 86400  # a restore test older than this is worth a nudge
# A run may start up to six hours after the window opens: hosts take turns, one
# at a time, so the last of fifteen guests still gets its turn before morning.
WINDOW = 6 * 3600
GUESTS = "pve:guests"  # a Proxmox server's safety net: every guest on it backed up
STUCK = 2 * 3600
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULTS = {"enabled": False, "day": 1, "time": "03:00", "level": "full", "reboot": True,
            # The backup must be newer than backup_days for a run to go ahead.
            "backup": "", "backup_days": 8, "restore": "",
            # Patched by another tool (Ansible, unattended-upgrades, ...): Argus never
            # patches it, and shows how that tool's job is doing when one is named.
            "elsewhere": False, "elsewhere_job": "",
            # The owner's call: this host can be rebuilt, so it is patched without a
            # backup (decision 12: the owner says what counts as a safety net).
            "no_backup": False,
            # Backed up by something Argus cannot see (another person's hypervisor):
            # the owner's word, in their own words, shown as such.
            "outside_backup": "",
            # Urgent security fixes the same night, outside the weekly window.
            "urgent": True}

SCHEMA = """
CREATE TABLE IF NOT EXISTS patch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, requested REAL NOT NULL,
    finished REAL, result TEXT NOT NULL, level TEXT NOT NULL, reboot INTEGER NOT NULL,
    trigger TEXT NOT NULL, local_date TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS patch_runs_host ON patch_runs (host, id);
"""


def allowed(p: dict) -> str:
    what = "all updates" if p["level"] == "full" else "security updates"
    return what + (", and reboots when needed" if p["reboot"] else ", no reboots")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class Patching:
    def __init__(self, db: sqlite3.Connection, settings, rt, clock: Callable[[], float]) -> None:
        self.db, self.settings, self.rt, self.clock = db, settings, rt, clock
        self.db.executescript(SCHEMA)
        # Why a host's window passed without a run, for the page and the alert.
        self.skipped: dict[str, str] = {}
        # The local date each skip happened on: its alert lasts that day only.
        # A missed window is news once, not a reminder every few hours all week
        # (the page keeps saying it until the next run).
        self.skipped_on: dict[str, str] = {}
        # The local date a host was seen waiting inside its window. Only then can
        # its turn have "not come": a plan saved after the day's window closed
        # (or a host that only came back later) missed nothing to other hosts.
        self.waited: dict[str, str] = {}
        # Why an urgent fix could not go in that night, the same way.
        self.urgent_skipped: dict[str, str] = {}
        self.urgent = None  # argus.urgent.Urgent, set by the app

    def plan(self, host: str) -> dict:
        return {**DEFAULTS, **(self.settings.get(SETTINGS + host) or {})}

    def set_plan(self, host: str, plan: dict) -> None:
        self.settings.set(SETTINGS + host, {k: plan[k] for k in DEFAULTS if k in plan})

    def forget(self, host: str) -> None:
        """A removed host leaves nothing behind: a new machine enrolled later under
        the same name must not inherit its schedule, its backup or its runs (as a
        reused Proxmox VMID once inherited a removed guest's backup, 2026-11-02)."""
        self.settings.set(SETTINGS + host, None)
        self.settings.set(REQUEST + host, None)
        self.skipped.pop(host, None)
        self.skipped_on.pop(host, None)
        self.waited.pop(host, None)
        self.urgent_skipped.pop(host, None)
        with self.db:
            self.db.execute("DELETE FROM patch_runs WHERE host = ?", (host,))

    def rename(self, old: str, new: str) -> None:
        """A renamed host keeps its schedule and runs, and guests whose backup its
        Proxmox server sees keep pointing at it (pve:<server>/<vmid>)."""
        for key in (SETTINGS, REQUEST):
            self.settings.set(key + new, self.settings.get(key + old))
            self.settings.set(key + old, None)
        for m in (self.skipped, self.skipped_on, self.waited):
            if old in m:
                m[new] = m.pop(old)
        with self.db:
            self.db.execute("UPDATE patch_runs SET host = ? WHERE host = ?", (new, old))
        keys = [r[0] for r in self.db.execute("SELECT key FROM settings WHERE key LIKE ?",
                                               (SETTINGS + "%",))]
        for k in keys:
            plan = self.settings.get(k)
            if isinstance(plan, dict) and plan.get("backup", "").startswith(f"pve:{old}/"):
                vmid = plan["backup"].split("/", 1)[1]
                self.settings.set(k, {**plan, "backup": f"pve:{new}/{vmid}"})

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
        elif kind == "pve":
            server, _, vmid = name.partition("/")
            px = (self.rt.companions.report_of(server) or {}).get("proxmox") or {}
            for g in px.get("guests") or []:
                if str(g["vmid"]) == vmid and g.get("backup"):
                    return now - g["backup"]["at"]
        return None

    def pve_backups(self, views: list[dict]) -> list[dict]:
        """Every guest the Proxmox servers report, with its newest backup, as refs
        a plan can name."""
        out = []
        for c in views:
            px = (c["report"] or {}).get("proxmox") or {}
            for g in px.get("guests") or []:
                b = g.get("backup") or {}
                out.append({"ref": f"pve:{c['name']}/{g['vmid']}", "server": c["name"],
                            "vmid": g["vmid"], "name": g["name"], "type": g["type"],
                            "status": g["status"], "at": b.get("at"),
                            "storage": b.get("storage", "")})
        return out

    def guests_net(self, host: str, report: dict | None, days: int) -> list[dict]:
        """A Proxmox server's guests and whether each is backed up recently enough.
        A guest marked as needing no backup (on the server's card) does not count."""
        px = (report or {}).get("proxmox") or {}
        ignored = set(self.rt.companions.ignored_of(host))
        now, out = self.rt.clock(), []
        for g in px.get("guests") or []:
            age = now - g["backup"]["at"] if g.get("backup") else None
            exempt = f"nobackup:{g['vmid']}" in ignored
            out.append({"vmid": g["vmid"], "name": g["name"], "type": g["type"],
                        "status": g["status"], "age": age, "exempt": exempt,
                        "ok": exempt or (age is not None and age < days * 86400)})
        return out

    @staticmethod
    def suggest(host: str, report: dict | None, backups: list[dict]) -> str:
        """The backup of the Proxmox guest this host is: the one with its name. A
        Proxmox server's own safety net is its guests' backups."""
        if (report or {}).get("proxmox"):
            return GUESTS
        names = {host.lower(), ((report or {}).get("hostname") or "").split(".")[0].lower()}
        for b in backups:
            if b["server"] != host and b["name"] and b["name"].lower() in names:
                return b["ref"]
        return ""

    def safety(self, host: str, report: dict | None) -> dict:
        """The host's safety net and what it allows."""
        p = self.plan(host)
        snapshot = (report or {}).get("snapshot") or ""
        if p["outside_backup"]:
            return {"net": f"a backup outside Argus (your word): {p['outside_backup']}",
                    "ok": True, "snapshot": "", "backup": "", "backup_age": None,
                    "fresh": False, "backup_days": p["backup_days"], "outside": True,
                    "restore": "", "tested_age": None, "tested": False}
        if p["no_backup"]:
            return {"net": "none, by your choice: it can be rebuilt", "ok": True,
                    "snapshot": "", "backup": "", "backup_age": None, "fresh": False,
                    "backup_days": p["backup_days"], "no_backup": True, "restore": "",
                    "tested_age": None, "tested": False}
        if p["backup"] == GUESTS or (not p["backup"] and (report or {}).get("proxmox")):
            guests = self.guests_net(host, report, p["backup_days"])
            short = [g["name"] or str(g["vmid"]) for g in guests if not g["ok"]]
            counted = [g for g in guests if not g["exempt"]]
            net = (f"every guest on it backed up within {p['backup_days']} days ({len(counted)})"
                   if guests and not short else
                   f"{len(short)} guest{'s' if len(short) != 1 else ''} without a recent backup: "
                   + ", ".join(short) if short else "no guests reported yet")
            ok = bool(guests) and not short
            return {"net": net, "ok": ok, "snapshot": "", "backup": GUESTS, "backup_age": None,
                    "fresh": ok, "backup_days": p["backup_days"], "guests": guests,
                    "restore": "", "tested_age": None, "tested": False}
        # Nothing linked: a Proxmox guest's own backup, the one the page shows, counts.
        ref = p["backup"] or self.suggest(host, report, self.pve_backups(
            self.rt.companions.views()))
        backup_age = self._age(ref) if ref else None
        tested_age = self._age(p["restore"]) if p["restore"] else None
        fresh = backup_age is not None and backup_age < p["backup_days"] * 86400
        tested = tested_age is not None and tested_age < TESTED
        if snapshot:
            net = f"a {snapshot} snapshot before each run"
        elif fresh:
            net = "a recent backup"
        elif ref and backup_age is not None:
            net = f"its backup is {int(backup_age // 86400)} days old (limit {p['backup_days']})"
        elif p["backup"]:
            net = "its backup has never reported"
        else:
            net = "none: link a backup to patch this host"
        return {"net": net, "ok": bool(snapshot) or fresh, "snapshot": snapshot,
                "backup": ref, "backup_age": backup_age, "fresh": fresh,
                "backup_days": p["backup_days"],
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
            cur = self.db.execute(
                "UPDATE patch_runs SET result = ?, summary = ?, log = ?, finished = ?"
                " WHERE id = ? AND host = ? AND result IN ('granted', 'running')",
                ("running" if not done else state, str(rep.get("summary", ""))[:500],
                 str(rep.get("log", ""))[-32 * 1024:], self.clock() if done else None,
                 int(rep["run"]), host))
        if done and cur.rowcount:
            whys = dict(self.settings.get(URGENT_WHY) or {})
            what = whys.pop(str(int(rep["run"])), None)
            if what is not None:
                self.settings.set(URGENT_WHY, whys or None)
                if state == "ok":  # a failed run is a problem, told as one
                    notices = list(self.settings.get(NOTICES) or [])
                    notices.append(f"Urgent security fix installed on {host}: {what}. "
                                   f"{str(rep.get('summary', ''))[:300]}".strip())
                    self.settings.set(NOTICES, notices[-20:])

    def take_notices(self) -> list[str]:
        """Good news to send once (the alerter drains it)."""
        notices = list(self.settings.get(NOTICES) or [])
        if notices:
            self.settings.set(NOTICES, None)
        return notices

    def expire(self) -> None:
        """A run with no result for two hours failed: free the lock and say so."""
        with self.db:
            self.db.execute(
                "UPDATE patch_runs SET result = 'failed', finished = ?,"
                " summary = 'no result from the host for two hours' WHERE result IN"
                " ('granted', 'running') AND requested < ?", (self.clock(), self.clock() - STUCK))

    def _skip(self, host: str, why: str, date: str) -> None:
        if self.skipped.get(host) != why:
            self.skipped_on[host] = date
        self.skipped[host] = why

    def grant(self, host: str, read_only: bool, report: dict | None) -> dict | None:
        """A run for this host now, or None. Records why a due window was skipped."""
        self.expire()
        p = self.plan(host)
        if p["elsewhere"]:
            self.settings.set(REQUEST + host, None)
            return None  # another tool patches this host; two would fight
        local = (report or {}).get("local") or {}
        manual = bool(self.settings.get(REQUEST + host))
        if not (p["enabled"] or manual) or read_only or not local.get("date"):
            return None
        weekday, minute = int(local.get("weekday", -1)), _minutes(local.get("time", "00:00"))
        start = _minutes(p["time"])
        in_window = weekday == p["day"] and start <= minute < start + WINDOW // 60
        done_today = self.db.execute(
            "SELECT 1 FROM patch_runs WHERE host = ? AND local_date = ? AND trigger = 'window'",
            (host, local["date"])).fetchone()
        if p["enabled"] and in_window:
            self.waited[host] = local["date"]
        if (p["enabled"] and not manual and not done_today and weekday == p["day"]
                and minute >= start + WINDOW // 60 and host not in self.skipped
                and self.waited.get(host) == local["date"]):
            # It waited all window and its turn never came: other hosts took it.
            self._skip(host, "its window ended before its turn came (other hosts took it)",
                       local["date"])
        if not (manual or (p["enabled"] and in_window)):
            return self._urgent_grant(host, p, report, local, weekday, minute, start)
        if not manual and done_today:
            return None
        safety = self.safety(host, report)
        why = self._blocked(host, p, safety)
        if why:
            self._skip(host, why, local["date"])
            if manual:
                self.settings.set(REQUEST + host, None)
            return None
        if self.running() is not None:
            return None  # one host at a time: this one waits its turn, quietly
        self.skipped.pop(host, None)
        level, reboot = p["level"], bool(p["reboot"])
        trigger = "manual" if manual else "window"
        with self.db:
            cur = self.db.execute(
                "INSERT INTO patch_runs (host, requested, result, level, reboot, trigger,"
                " local_date) VALUES (?, ?, 'granted', ?, ?, ?, ?)",
                (host, self.clock(), level, int(reboot), trigger, local["date"]))
        self.settings.set(REQUEST + host, None)
        return {"run": cur.lastrowid, "level": level, "reboot": reboot,
                "snapshot": bool(safety["snapshot"])}

    def _blocked(self, host: str, p: dict, safety: dict) -> str | None:
        """Why this host may not be patched now, whatever asked for the run."""
        if p["backup"] == GUESTS:
            # One Proxmox server at a time: never while another's newest run failed.
            for other in self.rt.companions.views():
                if other["name"] != host and (other["report"] or {}).get("proxmox"):
                    last = self.runs(other["name"], 1)
                    if last and last[0]["result"] == "failed":
                        return (f"the last run on {other['name']}, another Proxmox server,"
                                " failed; fix it first")
        if safety["ok"]:
            return None
        age = safety["backup_age"]
        return (safety["net"] if p["backup"] == GUESTS
                else f"its backup ({safety['backup']}) is {int(age // 86400)} days old,"
                f" over its limit of {p['backup_days']}" if age is not None
                else f"its backup ({p['backup']}) has never reported" if p["backup"]
                else "it has no backup linked")

    def _fix_waiting(self, host: str) -> str:
        """Why a pending fix at or above the owner's alert line (or for a flaw
        being exploited) will not go in tonight, or "" when there is none or it
        will: an urgent run takes it at the host's usual hour."""
        if self.urgent is None:
            return ""
        report = self.rt.companions.report_of(host) or {}
        hot = self.urgent.worth_alerting(
            ((report.get("updates") or {}).get("packages")) or [])
        if not hot:
            return ""
        p = self.plan(host)
        # Fixes an urgent run installs tonight need nobody; ones it could not
        # install already have their own alert (patch-urgent).
        tonight = {u["name"] for u in self.urgent_pending(host, report)} \
            if p["enabled"] and p["urgent"] and not report.get("proxmox") else set()
        if host in self.urgent_skipped:
            tonight |= {u["name"] for u in self.urgent_pending(host, report)}
        rest = [x for x in hot if x["name"] not in tonight]
        if not rest:
            return ""
        if p["elsewhere"]:
            when = "Something else patches this host; make sure it takes this fix."
        elif not p["enabled"]:
            when = "Argus does not patch this host, so install it there, or turn patching on."
        elif report.get("proxmox"):
            when = (f"A Proxmox server is updated as a whole: it goes in with the run on "
                    f"{DAYS[p['day']]} at {p['time']}, or press Patch now.")
        else:
            when = (f"It goes in with the weekly run on {DAYS[p['day']]} at {p['time']}, "
                    "or press Patch now.")
        return f"{self.urgent.reason(rest)}. {when}"

    def urgent_pending(self, host: str, report: dict | None) -> list[dict]:
        """This host's pending updates that cannot wait (argus.urgent.Urgent.of)."""
        if self.urgent is None:
            return []
        return self.urgent.of(((report or {}).get("updates") or {}).get("packages") or [])

    def _urgent_grant(self, host: str, p: dict, report: dict | None, local: dict,
                      weekday: int, minute: int, start: int) -> dict | None:
        """A run for just the urgent fixes, at the host's usual hour on any day."""
        pending = self.urgent_pending(host, report) if p["enabled"] and p["urgent"] else []
        if not pending:
            self.urgent_skipped.pop(host, None)
            return None
        if not start <= minute < start + WINDOW // 60:
            return None  # tonight, at its usual hour: not in the middle of the day
        if self.db.execute(
                "SELECT 1 FROM patch_runs WHERE host = ? AND local_date = ?"
                " AND trigger IN ('urgent', 'window')", (host, local["date"])).fetchone():
            return None  # one try a night; the weekly run took them on its own day
        what = self.urgent.reason(pending)
        if (report or {}).get("proxmox"):
            # A Proxmox server is only ever updated as a whole (dist-upgrade), never
            # a package at a time: the owner hears about the fix and decides.
            self.urgent_skipped[host] = (
                f"{what} waits for its weekly run: a Proxmox server is only updated as a "
                "whole, so Argus installs no single package on it. Press Patch now to "
                "take it sooner")
            return None
        safety = self.safety(host, report)
        why = self._blocked(host, p, safety)
        if why:
            self.urgent_skipped[host] = f"{what} could not be installed: {why}"
            return None
        if self.running() is not None:
            return None
        self.urgent_skipped.pop(host, None)
        reboot = bool(p["reboot"])
        with self.db:
            cur = self.db.execute(
                "INSERT INTO patch_runs (host, requested, result, level, reboot, trigger,"
                " local_date) VALUES (?, ?, 'granted', 'urgent', ?, 'urgent', ?)",
                (host, self.clock(), int(reboot), local["date"]))
        self.settings.set(URGENT_WHY, {**(self.settings.get(URGENT_WHY) or {}),
                                       str(cur.lastrowid): what})
        return {"run": cur.lastrowid, "level": "urgent", "reboot": reboot,
                "snapshot": bool(safety["snapshot"]),
                "packages": [u["name"] for u in pending][:100]}

    def elsewhere(self, p: dict) -> dict | None:
        """How the other tool's job is doing, when the plan names one."""
        ref = p["elsewhere_job"]
        if not p["elsewhere"] or not ref:
            return None
        kind, _, name = ref.partition(":")
        if kind == "job":
            for j in self.rt.job_views():
                if j["name"] == name:
                    last = j["last"]
                    return {"ref": ref, "title": j["title"] or name, "schedule": j["schedule"],
                            "status": j["status"], "at": last and last["finished"],
                            "summary": (last or {}).get("summary", "")}
        elif kind == "heartbeat":
            for h in self.rt.heartbeat_views():
                if h["name"] == name:
                    return {"ref": ref, "title": name, "schedule": "", "status": h["status"],
                            "at": h["last_seen"], "summary": ""}
        return {"ref": ref, "title": name, "schedule": "", "status": "never", "at": None,
                "summary": f"{ref} is not in the config"}

    def counts(self, host: str) -> bool:
        """Whether the host is patched on a schedule, by Argus or by something else."""
        p = self.plan(host)
        return bool(p["enabled"] or p["elsewhere"])

    def view(self, host: str, report: dict | None, backups: list[dict] | None = None) -> dict:
        p = self.plan(host)
        safety = self.safety(host, report)
        runs = self.runs(host)
        urgent = {"pending": self.urgent_pending(host, report),
                  "skipped": self.urgent_skipped.get(host),
                  "data": self.urgent.status() if self.urgent is not None else None,
                  "scores": self.urgent.scores_of(
                      ((report or {}).get("updates") or {}).get("packages") or [])
                  if self.urgent is not None else {}}
        requested = bool(self.settings.get(REQUEST + host))
        busy = self.running() if requested else None
        return {"plan": p, "safety": safety, "runs": runs, "elsewhere": self.elsewhere(p),
                "urgent": urgent,
                "suggested_backup": self.suggest(host, report, backups or []),
                "skipped": self.skipped.get(host), "requested": requested,
                # One host at a time: the run a requested one waits behind.
                "waiting_for": busy["host"] if busy and busy["host"] != host else None,
                "allowed": allowed(p) if safety["ok"] else "nothing until it has a backup"}

    def problems(self, hosts: list[str]) -> list[tuple[str, str, str]]:
        """(key, title, detail) for the alerter: a failed newest run, a skipped window."""
        out = []
        for h in hosts:
            last = self.runs(h, 1)
            if last and last[0]["result"] == "failed":
                out.append((f"patch:{h}", f"patching {h} failed",
                            last[0]["summary"] or "See the Hosts page."))
            today = ((self.rt.companions.report_of(h) or {}).get("local") or {}).get("date")
            if h in self.skipped and self.skipped_on.get(h) == today:
                out.append((f"patch-skipped:{h}", f"{h} was not patched",
                            f"Its window passed: {self.skipped[h]}."))
            if h in self.urgent_skipped:
                out.append((f"patch-urgent:{h}", f"{h} has an urgent security fix waiting",
                            f"{self.urgent_skipped[h]}."))
            waiting = self._fix_waiting(h)
            if waiting:
                out.append((f"patch-cve:{h}", f"{h} has a security fix waiting", waiting))
            # An urgent fix went in but only takes effect after a reboot the plan
            # does not allow: the owner's move.
            if last and last[0]["trigger"] == "urgent" and last[0]["result"] == "ok" \
                    and (self.rt.companions.report_of(h) or {}).get("reboot_required"):
                out.append((f"patch-reboot:{h}", f"{h} needs a reboot to finish an urgent fix",
                            "The fix is installed and takes effect after a reboot, which "
                            "this host's plan does not allow. Reboot it when you can."))
        return out
