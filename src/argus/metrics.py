"""Prometheus /metrics, built from the store at scrape time so it can never
disagree with what the API shows.

Alerting lives in dmb-monitoring. argus_scheduler_last_tick_timestamp_seconds is
there so a hung loop is distinguishable from a quiet week: every other series
here would keep reporting the last state forever.
"""

from __future__ import annotations

import os

from prometheus_client.core import CollectorRegistry, GaugeMetricFamily, InfoMetricFamily

from argus.github import Poller
from argus.runtime import Runtime

LABELS = ["check", "kind", "group", "site", "severity", "target"]
HB_LABELS = ["heartbeat", "group", "site", "severity"]
# Not "job": Prometheus sets that label itself (the scrape job) and would
# rename this one to exported_job.
JOB_LABELS = ["job_name", "group", "site", "severity"]


class Collector:
    def __init__(self, runtime: Runtime, poller: Poller | None = None) -> None:
        self.rt = runtime
        self.gh = poller

    def collect(self):
        rt = self.rt
        states = rt.store.all()

        up = GaugeMetricFamily(
            "argus_check_up",
            "0 when the check is down (3+ consecutive failures), else 1. "
            "Absent until the first result.",
            labels=LABELS,
        )
        fails = GaugeMetricFamily(
            "argus_check_consecutive_failures", "Failures since the last success.", labels=LABELS
        )
        dur = GaugeMetricFamily(
            "argus_check_duration_seconds", "How long the last probe took.", labels=LABELS
        )
        last = GaugeMetricFamily(
            "argus_check_last_run_timestamp_seconds", "When the check last ran.", labels=LABELS
        )
        for c in rt.config.checks:
            s = states.get(c.name)
            if s is None or s.last_run is None:
                continue
            lv = [c.name, c.kind, c.group, c.site, c.severity, c.target]
            up.add_metric(lv, 0 if s.status == "down" else 1)
            fails.add_metric(lv, s.consecutive_failures)
            dur.add_metric(lv, s.last_duration or 0)
            last.add_metric(lv, s.last_run)
        yield from (up, fails, dur, last)

        seen = GaugeMetricFamily(
            "argus_heartbeat_last_seen_timestamp_seconds",
            "Last beat received. Absent if none since the heartbeat was configured.",
            labels=HB_LABELS,
        )
        max_age = GaugeMetricFamily(
            "argus_heartbeat_max_age_seconds", "Seconds without a beat before it is late.",
            labels=HB_LABELS,
        )
        beats = rt.store.heartbeats()
        for h in rt.config.heartbeats:
            lv = [h.name, h.group, h.site, h.severity]
            max_age.add_metric(lv, h.max_age)
            if h.name in beats:
                seen.add_metric(lv, beats[h.name]["last_seen"])
        yield from (seen, max_age)

        result = GaugeMetricFamily(
            "argus_job_last_result", "1 if the job's newest run succeeded, 0 if it failed. "
            "Absent until the first report.", labels=JOB_LABELS,
        )
        ran = GaugeMetricFamily(
            "argus_job_last_run_timestamp_seconds", "When the newest run finished.",
            labels=JOB_LABELS,
        )
        ok = GaugeMetricFamily(
            "argus_job_last_success_timestamp_seconds", "When the newest successful run finished.",
            labels=JOB_LABELS,
        )
        job_age = GaugeMetricFamily(
            "argus_job_max_age_seconds", "Seconds without a run before it is late.",
            labels=JOB_LABELS,
        )
        runs = rt.store.last_runs()
        for j in rt.config.jobs:
            lv = [j.name, j.group, j.site, j.severity]
            job_age.add_metric(lv, j.max_age)
            run = runs.get(j.name)
            if run is None:
                continue
            result.add_metric(lv, 1 if run["result"] == "ok" else 0)
            ran.add_metric(lv, run["finished"])
            if run["last_success"] is not None:
                ok.add_metric(lv, run["last_success"])
        yield from (result, ran, ok, job_age)

        yield GaugeMetricFamily(
            "argus_scheduler_last_tick_timestamp_seconds",
            "Last scheduler loop. Stale means checks have stopped running.",
            value=rt.last_tick,
        )
        yield GaugeMetricFamily(
            "argus_config_last_reload_success",
            "0 if the newest argus.yml was rejected; the previous config is still running.",
            value=1 if rt.reload_ok else 0,
        )
        yield GaugeMetricFamily(
            "argus_config_checks", "Checks in the running config.", value=len(rt.config.checks)
        )
        if self.gh is not None:
            yield from self.github()
        info = InfoMetricFamily("argus_build", "Running build and config.")
        info.add_metric([], {
            "version": os.environ.get("ARGUS_VERSION", "dev"),
            "commit": os.environ.get("ARGUS_COMMIT", "unknown"),
            "config": rt.config_hash,
        })
        yield info


    def github(self):
        """The app pipeline (decision 0010). Absent until the first good poll."""
        gh = self.gh
        if gh.last_success is not None:
            yield GaugeMetricFamily(
                "argus_github_last_poll_success_timestamp_seconds",
                "Last good poll of GitHub. Stale means the repo series below are old.",
                value=gh.last_success,
            )
        alerts = GaugeMetricFamily(
            "argus_repo_open_alerts", "Open Dependabot security alerts.",
            labels=["repo", "severity"],
        )
        oldest = GaugeMetricFamily(
            "argus_repo_oldest_open_alert_timestamp_seconds",
            "When the oldest open alert of this severity was opened.", labels=["repo", "severity"],
        )
        prs = GaugeMetricFamily(
            "argus_repo_update_prs", "Open Dependabot PRs: waiting (a major, for Devin), "
            "failing (CI red) or open (in CI or about to merge).", labels=["repo", "state"],
        )
        ci = GaugeMetricFamily(
            "argus_repo_main_ci_ok", "1 if the newest ci.yml run on main passed, 0 if it failed. "
            "Absent while it runs or with no ci.yml.", labels=["repo"],
        )
        for name, s in gh.repos.items():
            for sev in SEVERITIES:
                found = [a for a in s["alerts"] or [] if a["severity"] == sev]
                if s["alerts"] is not None:
                    alerts.add_metric([name, sev], len(found))
                if found:
                    oldest.add_metric([name, sev], min(a["created_at"] for a in found))
            counts = {"waiting": 0, "failing": 0, "open": 0}
            for p in s["updates"]:
                if p["waits_for_devin"]:
                    counts["waiting"] += 1
                else:
                    counts["failing" if p["ci"] == "failure" else "open"] += 1
            for state, n in counts.items():
                prs.add_metric([name, state], n)
            if s["main"] and s["main"]["status"] != "running":
                ci.add_metric([name], 1 if s["main"]["status"] == "success" else 0)
        yield from (alerts, oldest, prs, ci)


SEVERITIES = ("critical", "high", "medium", "low")


def registry(runtime: Runtime, poller: Poller | None = None) -> CollectorRegistry:
    reg = CollectorRegistry(auto_describe=False)
    reg.register(Collector(runtime, poller))
    return reg
