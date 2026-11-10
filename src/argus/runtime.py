"""The scheduler: runs each check on its interval, reloads argus.yml when it
changes, and records heartbeats and job results.

Config reload is by mtime, checked every tick, so a `git pull` of homelab-ops is
the whole deploy for a config change. An invalid file is logged and ignored;
the running config stays and argus_config_last_reload_success drops to 0.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from argus import config as cfg
from argus.companions import Companions
from argus.probes import Prober, Result
from argus.state import Store

log = logging.getLogger("argus")

TICK = 1.0
MAX_CONCURRENT = 16


class Runtime:
    def __init__(
        self,
        config_path: Path,
        db_path: Path | str,
        prober: Prober | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config_path = config_path
        self.db_path = Path(db_path)
        self.clock = clock
        self.store = Store(db_path)
        self.companions = Companions(self.store.db, clock)
        self.prober = prober or Prober()
        self.config, self.config_hash, self.config_mtime = cfg.Config(), "none", None
        self.reload_ok = True
        self.config_text: str | None = None  # set when the config comes from Argus or git
        self.watch_file = True  # the mounted file is the source (configsrc decides)
        self.started = self.last_reload = clock()
        if config_path.exists():
            self.config, self.config_hash = cfg.load(config_path)
            self.config_mtime = config_path.stat().st_mtime
        self.last_tick = 0.0
        self.next_due: dict[str, float] = {}
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._running: set[asyncio.Task] = set()
        self.store.prune(self.names())

    def names(self) -> set[str]:
        return ({c.name for c in self.config.checks} | {h.name for h in self.config.heartbeats}
                | {j.name for j in self.config.jobs})

    def apply_text(self, text: str) -> None:
        """Take a config from the database or git. Raises when it is invalid; the
        running config then stays."""
        new = cfg.parse(text)
        self.config, self.config_text, self.reload_ok = new, text, True
        self.config_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
        self.watch_file, self.last_reload = False, self.clock()
        self.next_due = {k: v for k, v in self.next_due.items() if k in self.names()}
        self.store.prune(self.names())
        log.info("config applied: %s, %d checks", self.config_hash, len(new.checks))

    def maybe_reload(self) -> None:
        if not self.watch_file:
            return
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            # No argus.yml is a valid setup (everything from the UI); one that
            # was there and went away keeps the running config.
            return
        except OSError as e:
            if self.reload_ok:
                log.error("config unreadable, keeping the running one: %s", e)
            self.reload_ok = False
            return
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            new, digest = cfg.load(self.config_path)
        except Exception as e:
            log.error("config rejected, keeping the running one: %s", e)
            self.reload_ok = False
            return
        self.config, self.config_hash, self.reload_ok = new, digest, True
        self.last_reload = self.clock()
        self.next_due = {k: v for k, v in self.next_due.items() if k in self.names()}
        self.store.prune(self.names())
        log.info("config reloaded: %s, %d checks", digest, len(new.checks))

    async def _run(self, check: cfg.Check) -> None:
        async with self._sem:
            result: Result = await self.prober.run(check)
        before = self.store.get(check.name).status
        state = self.store.record(check.name, result, self.clock())
        if state.status != before:
            log.info("%s: %s -> %s %s", check.name, before, state.status, result.error)

    def tick(self) -> list[asyncio.Task]:
        """Start every check that is due. Returns the tasks started (for tests)."""
        now = self.clock()
        self.maybe_reload()
        started = []
        for check in self.config.checks:
            if check.companion:
                continue  # its companion runs it and reports the results
            if check.name not in self.next_due:
                # Spread first runs over 10 s so a restart is not one burst.
                self.next_due[check.name] = now + zlib.crc32(check.name.encode()) % 10
            if self.next_due[check.name] > now:
                continue
            self.next_due[check.name] = now + check.interval
            task = asyncio.create_task(self._run(check))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
            started.append(task)
        self.last_tick = now
        return started

    async def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(TICK)

    def heartbeat_views(self) -> list[dict]:
        """Each configured heartbeat with its state: ok, late or never."""
        beats, now = self.store.heartbeats(), self.clock()
        out = []
        for h in self.config.heartbeats:
            b = beats.get(h.name)
            age = now - b["last_seen"] if b else None
            status = "never" if b is None else ("late" if age > h.max_age else "ok")
            out.append({**h.model_dump(), "status": status, "age": age,
                        "last_seen": b and b["last_seen"], "source": b and b["source"],
                        "version": b and b["version"], "version_at": b and b["version_at"]})
        return out

    def job_views(self) -> list[dict]:
        """Each configured job with its newest run and state: ok, failed, late or never."""
        last, now = self.store.last_runs(), self.clock()
        out = []
        for j in self.config.jobs:
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

    def companion_checks(self, companion: str) -> list[cfg.Check]:
        return [c for c in self.config.checks if c.companion == companion]

    def record_remote(self, companion: str, name: str, result: Result) -> bool:
        """A result a companion reports; only for checks assigned to it."""
        if not any(c.name == name for c in self.companion_checks(companion)):
            return False
        before = self.store.get(name).status
        state = self.store.record(name, result, self.clock())
        if state.status != before:
            log.info("%s (from %s): %s -> %s %s", name, companion, before, state.status,
                     result.error)
        return True

    def beat(self, name: str, source: str, version: str | None = None) -> bool:
        if name not in {h.name for h in self.config.heartbeats}:
            return False
        self.store.beat(name, self.clock(), source, version)
        return True

    def report(self, name: str, run: dict, source: str) -> bool:
        if name not in {j.name for j in self.config.jobs}:
            return False
        self.store.record_run(name, run, self.clock(), source)
        return True

    async def aclose(self) -> None:
        for task in list(self._running):
            task.cancel()
        await self.prober.aclose()
        self.store.close()
