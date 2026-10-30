"""Check, heartbeat and job state in SQLite, so a restart neither forgets an outage
nor resets the failure count that decides one.

A check is `up` after any success, `failing` after 1 or 2 consecutive failures,
and `down` from the third. `unknown` means no result yet.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from argus.probes import Result

DOWN_AFTER = 3
KEEP_TRANSITIONS = 2000
KEEP_RUNS = 20  # per job

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    last_run REAL, last_ok REAL, last_change REAL,
    last_error TEXT NOT NULL DEFAULT '', last_duration REAL
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, ts REAL NOT NULL,
    from_status TEXT NOT NULL, to_status TEXT NOT NULL, error TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transitions_name ON transitions (name, ts);
CREATE TABLE IF NOT EXISTS heartbeats (
    name TEXT PRIMARY KEY, last_seen REAL NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, received REAL NOT NULL, started REAL NOT NULL, finished REAL NOT NULL,
    result TEXT NOT NULL, summary TEXT NOT NULL, log TEXT NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_runs_name ON job_runs (name, id);
"""


@dataclass(frozen=True)
class CheckState:
    name: str
    status: str = "unknown"
    consecutive_failures: int = 0
    last_run: float | None = None
    last_ok: float | None = None
    last_change: float | None = None
    last_error: str = ""
    last_duration: float | None = None


class Store:
    def __init__(self, path: Path | str) -> None:
        # One thread in production (the event loop); tests drive the app from another.
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Added after v1: the version a deploying timer reports and when it first did;
        # what each job run covered.
        for table, col in (("heartbeats", "version TEXT"), ("heartbeats", "version_at REAL"),
                           ("job_runs", "items TEXT NOT NULL DEFAULT '[]'")):
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col.split()[0] not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

    def close(self) -> None:
        self.db.close()

    def get(self, name: str) -> CheckState:
        row = self.db.execute("SELECT * FROM checks WHERE name = ?", (name,)).fetchone()
        return CheckState(**dict(row)) if row else CheckState(name)

    def all(self) -> dict[str, CheckState]:
        return {r["name"]: CheckState(**dict(r)) for r in self.db.execute("SELECT * FROM checks")}

    def record(self, name: str, result: Result, now: float) -> CheckState:
        prev = self.get(name)
        if result.ok:
            failures, status, last_ok = 0, "up", now
        else:
            failures = prev.consecutive_failures + 1
            status = "down" if failures >= DOWN_AFTER else "failing"
            last_ok = prev.last_ok
        changed = status != prev.status
        state = CheckState(
            name=name,
            status=status,
            consecutive_failures=failures,
            last_run=now,
            last_ok=last_ok,
            last_change=now if changed else prev.last_change,
            last_error=result.error,
            last_duration=result.duration,
        )
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO checks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (state.name, state.status, state.consecutive_failures, state.last_run,
                 state.last_ok, state.last_change, state.last_error, state.last_duration),
            )
            if changed:
                self.db.execute(
                    "INSERT INTO transitions (name, ts, from_status, to_status, error)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, now, prev.status, status, result.error),
                )
                self.db.execute(
                    "DELETE FROM transitions WHERE id <= (SELECT MAX(id) FROM transitions) - ?",
                    (KEEP_TRANSITIONS,),
                )
        return state

    def transitions(self, name: str, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, from_status, to_status, error FROM transitions"
            " WHERE name = ? ORDER BY id DESC LIMIT ?",
            (name, limit),
        )
        return [dict(r) for r in rows]

    def beat(self, name: str, now: float, source: str, version: str | None = None) -> None:
        """A timer ran. A timer that deploys may say which version runs now;
        version_at is when it first said so, which is when the deploy happened
        (to within the timer's interval)."""
        with self.db:
            old = self.db.execute("SELECT version, version_at FROM heartbeats WHERE name = ?",
                                  (name,)).fetchone()
            if version is None and old is not None:
                version, version_at = old["version"], old["version_at"]
            elif old is not None and old["version"] == version:
                version_at = old["version_at"]
            else:
                version_at = now if version else None
            self.db.execute(
                "INSERT OR REPLACE INTO heartbeats (name, last_seen, source, version, version_at)"
                " VALUES (?, ?, ?, ?, ?)", (name, now, source, version, version_at)
            )

    def heartbeats(self) -> dict[str, dict]:
        return {r["name"]: dict(r) for r in self.db.execute("SELECT * FROM heartbeats")}

    def record_run(self, name: str, run: dict, now: float, source: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO job_runs (name, received, started, finished, result, summary, log,"
                " source, items) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, now, run["started"], run["finished"], run["result"], run["summary"],
                 run["log"], source, json.dumps(run.get("items", []))),
            )
            self.db.execute(
                "DELETE FROM job_runs WHERE name = ? AND id NOT IN"
                " (SELECT id FROM job_runs WHERE name = ? ORDER BY id DESC LIMIT ?)",
                (name, name, KEEP_RUNS),
            )

    def runs(self, name: str, limit: int = KEEP_RUNS, with_log: bool = True) -> list[dict]:
        cols = "*" if with_log else ("id, name, received, started, finished, result, summary,"
                                     " source, items")
        rows = self.db.execute(
            f"SELECT {cols} FROM job_runs WHERE name = ? ORDER BY id DESC LIMIT ?", (name, limit)
        )
        return [{**dict(r), "items": json.loads(r["items"])} for r in rows]

    def last_runs(self) -> dict[str, dict]:
        """Each job's newest run and newest success (no logs)."""
        out: dict[str, dict] = {}
        for r in self.db.execute(
            "SELECT name, MAX(id) AS id FROM job_runs GROUP BY name"
        ).fetchall():
            last = self.db.execute(
                "SELECT id, received, started, finished, result, summary, items FROM job_runs"
                " WHERE id = ?", (r["id"],)).fetchone()
            ok = self.db.execute(
                "SELECT MAX(finished) FROM job_runs WHERE name = ? AND result = 'ok'",
                (r["name"],)).fetchone()[0]
            out[r["name"]] = {**dict(last), "items": json.loads(last["items"]), "last_success": ok}
        return out

    def prune(self, keep: set[str]) -> None:
        """Forget checks, heartbeats and jobs that left the config."""
        with self.db:
            for table in ("checks", "heartbeats", "transitions", "job_runs"):
                names = {r[0] for r in self.db.execute(f"SELECT DISTINCT name FROM {table}")}
                for gone in names - keep:
                    self.db.execute(f"DELETE FROM {table} WHERE name = ?", (gone,))
