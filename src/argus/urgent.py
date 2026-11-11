"""Which pending security fixes cannot wait for the weekly window (decision 22).

A fix is urgent when a CVE it closes is

- on CISA's Known Exploited Vulnerabilities list: someone is using it in real
  attacks now, whatever its score; or
- critical: a CVSS base score of 9.0 or more in its public CVE record, or the
  distro's own advisory calls it critical (dnf reports that with the update).

High (7.0 to 8.9) is not urgent by design: a kernel collects those most weeks,
and a rule that fires every week is the weekly window again.

Argus looks both up itself, from public sources that need no account, so a
companion needs no internet for this. Hosts already report the CVE ids their
pending updates fix (from the distro's changelogs); only those are asked about.
When a source does not answer, what Argus knew stays in use, and it says so.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable

import httpx

log = logging.getLogger("argus.urgent")

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
CVE_URL = "https://cveawg.mitre.org/api/cve/"
CRITICAL = 9.0
EVERY = 600  # how often the loop looks for work
KEV_EVERY = 6 * 3600
SCORED_AGAIN = 7 * 86400  # a score can change; ask again weekly
UNSCORED_AGAIN = 12 * 3600  # a new CVE often gets its score days later
PER_PASS = 40  # CVE records asked for in one pass, politely
STALE = 2 * 86400  # the exploited list older than this is worth saying
CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,7}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cve_scores (
    cve TEXT PRIMARY KEY, score REAL, checked REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cve_exploited (cve TEXT PRIMARY KEY);
"""
KEV_AT = "urgent.kev_at"


def base_score(record: dict) -> float | None:
    """The highest CVSS base score a CVE record gives: its publisher's or one
    added by CISA's enrichment (the record's adp containers)."""
    containers = record.get("containers") or {}
    found = []
    for part in [containers.get("cna") or {}, *(containers.get("adp") or [])]:
        for metric in part.get("metrics") or []:
            for name, body in metric.items():
                if name.startswith("cvss") and isinstance(body, dict):
                    score = body.get("baseScore")
                    if isinstance(score, int | float) and 0 <= score <= 10:
                        found.append(float(score))
    return max(found) if found else None


class Urgent:
    def __init__(self, db: sqlite3.Connection, settings, client: httpx.AsyncClient,
                 clock: Callable[[], float], wanted: Callable[[], Iterable[str]]) -> None:
        self.db, self.settings, self.client, self.clock = db, settings, client, clock
        self.wanted = wanted  # the CVE ids pending on any host now
        self.error = ""
        with self.db:
            self.db.executescript(SCHEMA)
        self.exploited = {r[0] for r in self.db.execute("SELECT cve FROM cve_exploited")}
        self.scores: dict[str, tuple[float | None, float]] = {
            r[0]: (r[1], r[2])
            for r in self.db.execute("SELECT cve, score, checked FROM cve_scores")}

    # --- what is urgent ---

    def why(self, cve: str) -> str:
        """"exploited", "critical" or ""."""
        if cve in self.exploited:
            return "exploited"
        score = self.scores.get(cve, (None, 0))[0]
        return "critical" if score is not None and score >= CRITICAL else ""

    def of(self, packages: list[dict]) -> list[dict]:
        """The pending security updates that are urgent, each with the CVEs that
        make it so: [{name, cves: [{id, why, score}], vendor}]."""
        out = []
        for p in packages or []:
            if not p.get("security"):
                continue
            cves = [{"id": c, "why": w, "score": self.scores.get(c, (None, 0))[0]}
                    for c in p.get("cves") or [] if (w := self.why(c))]
            vendor = str(p.get("severity", "")).lower() == "critical"
            if cves or vendor:
                out.append({"name": p["name"], "cves": cves, "vendor": vendor})
        return out

    def scores_of(self, packages: list[dict]) -> dict[str, float]:
        """The CVSS base score of each CVE these updates fix, where known, so a
        person can see how serious each one is (not only the urgent ones)."""
        return {c: score for p in packages or [] for c in p.get("cves") or []
                if (score := self.scores.get(c, (None, 0))[0]) is not None}

    @staticmethod
    def reason(urgent: list[dict]) -> str:
        """One line for a message: the packages and why they could not wait."""
        names = ", ".join(u["name"] for u in urgent[:6]) + (
            f" and {len(urgent) - 6} more" if len(urgent) > 6 else "")
        cves = {c["id"]: c["why"] for u in urgent for c in u["cves"]}
        exploited = sorted(c for c, w in cves.items() if w == "exploited")
        critical = sorted(c for c, w in cves.items() if w == "critical")
        parts = []
        if exploited:
            parts.append(f"{', '.join(exploited[:3])} {'is' if len(exploited) == 1 else 'are'}"
                         " being exploited")
        if critical:
            parts.append(f"{', '.join(critical[:3])} {'is' if len(critical) == 1 else 'are'}"
                         " rated critical")
        if not parts:
            parts.append("its distro rates the fix critical")
        return f"{names} ({'; '.join(parts)})"

    def status(self) -> dict:
        at = self.settings.get(KEV_AT)
        return {"exploited_at": at, "exploited": len(self.exploited), "error": self.error,
                "stale": at is None or self.clock() - at > STALE}

    # --- looking it up ---

    async def refresh(self) -> None:
        now = self.clock()
        errors = []
        at = self.settings.get(KEV_AT)
        if at is None or now - at >= KEV_EVERY:
            try:
                r = await self.client.get(KEV_URL, timeout=60, follow_redirects=True)
                r.raise_for_status()
                ids = {v["cveID"] for v in r.json()["vulnerabilities"]
                       if CVE_ID.match(str(v.get("cveID", "")))}
                if not ids:
                    raise ValueError("an empty list")
                with self.db:
                    self.db.execute("DELETE FROM cve_exploited")
                    self.db.executemany("INSERT INTO cve_exploited (cve) VALUES (?)",
                                        [(c,) for c in sorted(ids)])
                self.exploited = ids
                self.settings.set(KEV_AT, now)
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
                log.warning("the exploited-vulnerabilities list was not read: %s", e)
                errors.append("CISA's list of exploited vulnerabilities did not answer")
        due = []
        for cve in sorted({c for c in self.wanted() if CVE_ID.match(c)}):
            score, checked = self.scores.get(cve, (None, 0.0))
            if now - checked >= (UNSCORED_AGAIN if score is None else SCORED_AGAIN):
                due.append(cve)
        failed = 0
        for cve in due[:PER_PASS]:
            try:
                r = await self.client.get(CVE_URL + cve, timeout=20)
                if r.status_code == 404:
                    score = None  # reserved or not published yet
                else:
                    r.raise_for_status()
                    score = base_score(r.json())
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                failed += 1
                if failed >= 3:
                    break  # the source is down: try again next pass
                continue
            self.scores[cve] = (score, now)
            with self.db:
                self.db.execute(
                    "INSERT INTO cve_scores (cve, score, checked) VALUES (?, ?, ?)"
                    " ON CONFLICT(cve) DO UPDATE SET score = excluded.score,"
                    " checked = excluded.checked", (cve, score, now))
            await asyncio.sleep(0.2)
        if failed:
            errors.append("the CVE database did not answer")
        self.error = "; ".join(errors)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - a lookup must never end the loop
                log.exception("urgent lookup failed")
            await asyncio.sleep(EVERY)
