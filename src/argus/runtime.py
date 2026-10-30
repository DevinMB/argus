"""The scheduler: runs each check on its interval, reloads argus.yml when it
changes, and records heartbeats and job results.

Config reload is by mtime, checked every tick, so a `git pull` of homelab-ops is
the whole deploy for a config change. An invalid file is logged and ignored;
the running config stays and argus_config_last_reload_success drops to 0.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from argus import config as cfg
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
        self.clock = clock
        self.store = Store(db_path)
        self.prober = prober or Prober()
        self.config, self.config_hash = cfg.load(config_path)
        self.config_mtime = config_path.stat().st_mtime
        self.reload_ok = True
        self.last_reload = clock()
        self.last_tick = 0.0
        self.next_due: dict[str, float] = {}
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._running: set[asyncio.Task] = set()
        self.store.prune(self.names())

    def names(self) -> set[str]:
        return ({c.name for c in self.config.checks} | {h.name for h in self.config.heartbeats}
                | {j.name for j in self.config.jobs})

    def maybe_reload(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
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
