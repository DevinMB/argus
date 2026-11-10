"""What a suggested port speaks, so Watch makes the right check.

A port that answers HTTP gets an HTTP check (it proves the app answers, not
only that something listens); anything else stays TCP. Argus knocks once on
each port it suggests, in the background while the page is open, and keeps
what it learned for a day. Ports only the host's own companion can reach are
left as TCP.
"""

from __future__ import annotations

import asyncio
import re
import time

import httpx

KEEP = 24 * 3600
PER_RUN = 24
TIMEOUT = 2.0

# Plumbing most people never want an alert for: metrics exporters, brokers'
# internals, log shippers. Shown last, folded, and never hidden.
INTERNAL = re.compile(r"exporter|cadvisor|zookeeper|kafka|promtail|watchtower|otel|collector"
                      r"|fluent|vector|node-red-?worker|registry-?cache", re.I)


def internal(container: str, image: str = "") -> bool:
    return bool(INTERNAL.search(container) or INTERNAL.search(image))


class Sniffer:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.seen: dict[str, dict] = {}  # "addr:port" -> {"scheme", "status", "at"}
        self.task: asyncio.Task | None = None
        self.client = httpx.AsyncClient(verify=False, timeout=TIMEOUT,
                                        headers={"User-Agent": "Argus"})

    def known(self, target: str) -> dict | None:
        r = self.seen.get(target)
        return r if r and self.clock() - r["at"] < KEEP else None

    def kick(self, targets: list[str]) -> None:
        """Look at the targets not known yet, in the background, once at a time."""
        todo = [t for t in dict.fromkeys(targets) if self.known(t) is None][:PER_RUN]
        if todo and (self.task is None or self.task.done()):
            self.task = asyncio.get_running_loop().create_task(self._run(todo))

    async def _run(self, targets: list[str]) -> None:
        await asyncio.gather(*(self._one(t) for t in targets), return_exceptions=True)

    async def _one(self, target: str) -> None:
        found = {"scheme": "", "status": 0, "at": self.clock()}
        for scheme in ("https", "http"):
            try:
                r = await self.client.get(f"{scheme}://{target}/")
            except (httpx.HTTPError, OSError):
                continue
            if r.status_code < 500:
                found.update(scheme=scheme, status=r.status_code)
                break
        self.seen[target] = found


def shape(check: dict, seen: dict | None) -> dict:
    """The suggested check as HTTP when the port answered HTTP."""
    if not seen or not seen["scheme"] or check.get("companion"):
        return check
    out = {**check, "kind": "http", "target": f"{seen['scheme']}://{check['target']}/"}
    if seen["status"] != 200:
        out["expect_status"] = [seen["status"]]  # a login redirect or 401 is the app answering
    if seen["scheme"] == "https":
        out["verify_tls"] = False  # most homelab apps make their own certificate
    return out
