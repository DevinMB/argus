"""Urgent security fixes do not wait for the weekly window (decision 22)."""

import asyncio

import httpx
import test_patching
from test_patching import PLAN, enroll, job_ok, report

from argus.urgent import CVE_URL, KEV_URL, Urgent, base_score

env = test_patching.env  # the same Argus, clock and signed-in admin

WED_0305 = {"weekday": 2, "time": "03:05", "date": "2027-01-13"}
THU_0305 = {"weekday": 3, "time": "03:05", "date": "2027-01-14"}


def pending(*pkgs):
    return {"pending": len(pkgs), "security": len(pkgs), "manager": "apt",
            "packages": [{"name": n, "to": "2", "security": True, "cves": cves}
                         for n, cves in pkgs]}


def test_an_exploited_fix_goes_in_that_night_and_only_it(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    c.put("/api/patching/nas", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    ups = pending(("openssl", ["CVE-2027-0001"]), ("libssl3", ["CVE-2027-0001"]),
                  ("vim", ["CVE-2027-0002"]))
    # Wednesday at its usual hour, but nothing is urgent: the weekly window is Tuesday.
    assert "patch" not in report(c, nas, local=WED_0305, updates=ups)
    c.app.state.urgent.exploited = {"CVE-2027-0001"}
    c.app.state.urgent.scores = {"CVE-2027-0002": (7.8, clock.now)}  # high is not urgent
    # Not in the middle of the day: tonight, at the host's usual hour.
    assert "patch" not in report(c, nas, local={**WED_0305, "time": "14:00"}, updates=ups)
    v = c.get("/api/companions").json()["companions"][0]["patching"]
    assert [u["name"] for u in v["urgent"]["pending"]] == ["openssl", "libssl3"]
    assert v["urgent"]["pending"][0]["cves"] == [
        {"id": "CVE-2027-0001", "why": "exploited", "score": None}]
    run = report(c, nas, local=WED_0305, updates=ups)["patch"]
    assert run["level"] == "urgent" and run["packages"] == ["openssl", "libssl3"]
    assert run["reboot"] is True  # as far as the plan allows, no further
    # One try a night.
    report(c, nas, local=WED_0305, updates=ups, patch={
        "run": run["run"], "state": "ok", "summary": "urgent security fixes: 2 package(s)"})
    assert "patch" not in report(c, nas, local={**WED_0305, "time": "04:00"}, updates=ups)
    last = c.get("/api/companions").json()["companions"][0]["patching"]["runs"][0]
    assert last["trigger"] == "urgent" and last["result"] == "ok"
    # Told once, as good news.
    (note,) = c.app.state.patching.take_notices()
    assert note.startswith("Urgent security fix installed on nas: openssl, libssl3 "
                           "(CVE-2027-0001 is being exploited). urgent security fixes")
    assert c.app.state.patching.take_notices() == []


def test_a_critical_score_or_the_distros_word_is_urgent_too(env):
    c, csrf, rt, clock = env
    u = c.app.state.urgent
    u.scores = {"CVE-2027-0003": (9.8, clock.now), "CVE-2027-0004": (8.9, clock.now)}
    got = u.of([
        {"name": "glibc", "security": True, "cves": ["CVE-2027-0003", "CVE-2027-0004"]},
        {"name": "curl", "security": True, "cves": ["CVE-2027-0004"]},
        {"name": "kernel", "security": True, "severity": "Critical", "cves": []},
        {"name": "bash", "security": False, "cves": ["CVE-2027-0003"]},  # not a security update
    ])
    assert [(g["name"], [x["why"] for x in g["cves"]], g["vendor"]) for g in got] == [
        ("glibc", ["critical"], False), ("kernel", [], True)]
    assert u.reason(got) == "glibc, kernel (CVE-2027-0003 is rated critical)"



def test_every_known_score_is_shown_not_only_the_urgent_ones(env):
    c, csrf, rt, clock = env
    u = c.app.state.urgent
    u.scores = {"CVE-2027-0003": (9.8, clock.now), "CVE-2027-0004": (5.3, clock.now),
                "CVE-2027-0005": (None, clock.now)}
    assert u.scores_of([
        {"name": "glibc", "security": True, "cves": ["CVE-2027-0003", "CVE-2027-0005"]},
        {"name": "perl", "security": True, "cves": ["CVE-2027-0004", "CVE-2027-0009"]},
    ]) == {"CVE-2027-0003": 9.8, "CVE-2027-0004": 5.3}


def test_urgent_runs_keep_every_safety_rule(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    c.put("/api/patching/nas", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    c.app.state.urgent.exploited = {"CVE-2027-0001"}
    ups = pending(("openssl", ["CVE-2027-0001"]))
    # A stale backup: no run, and the owner hears why.
    clock.now += 9 * 86400
    assert "patch" not in report(c, nas, local=WED_0305, updates=ups)
    problems = c.app.state.alerter.problems()
    assert problems["patch-urgent:nas"].title == "nas has an urgent security fix waiting"
    assert "openssl (CVE-2027-0001 is being exploited) could not be installed: its backup" \
        in problems["patch-urgent:nas"].detail
    # A fresh backup: it goes in the next night, and the problem clears.
    job_ok(c, rt, clock, "nas-backup")
    run = report(c, nas, local=THU_0305, updates=ups)["patch"]
    assert run["level"] == "urgent"
    assert "patch-urgent:nas" not in c.app.state.alerter.problems()
    # One host at a time: another host with the same fix waits its turn.
    b = enroll(c, csrf, "b")
    c.put("/api/patching/b", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    assert "patch" not in report(c, b, local=THU_0305, updates=ups)
    report(c, nas, local=THU_0305, patch={"run": run["run"], "state": "ok", "summary": "done"})
    assert report(c, b, local=THU_0305, updates=ups)["patch"]["level"] == "urgent"


def test_the_owner_can_turn_it_off_and_other_hosts_are_left_alone(env):
    c, csrf, rt, clock = env
    job_ok(c, rt, clock, "nas-backup")
    c.app.state.urgent.exploited = {"CVE-2027-0001"}
    ups = pending(("openssl", ["CVE-2027-0001"]))
    off = enroll(c, csrf, "off")
    c.put("/api/patching/off", headers=csrf,
          json={**PLAN, "backup": "job:nas-backup", "urgent": False})
    assert "patch" not in report(c, off, local=WED_0305, updates=ups)
    never = enroll(c, csrf, "never")  # patching was never turned on
    assert "patch" not in report(c, never, local=WED_0305, updates=ups)
    ro = enroll(c, csrf, "ro", read_only=True)  # monitoring only
    assert "patch" not in report(c, ro, local=WED_0305, updates=ups)
    assert not any(k.startswith("patch-urgent") for k in c.app.state.alerter.problems())


def test_a_fix_that_needs_a_reboot_the_plan_forbids_says_so(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    c.put("/api/patching/nas", headers=csrf,
          json={**PLAN, "backup": "job:nas-backup", "reboot": False})
    job_ok(c, rt, clock, "nas-backup")
    c.app.state.urgent.exploited = {"CVE-2027-0001"}
    ups = pending(("linux-image", ["CVE-2027-0001"]))
    run = report(c, nas, local=WED_0305, updates=ups)["patch"]
    assert run["reboot"] is False
    report(c, nas, local=WED_0305, reboot_required=True,
           patch={"run": run["run"], "state": "ok", "summary": "1 package(s) updated"})
    p = c.app.state.alerter.problems()["patch-reboot:nas"]
    assert p.title == "nas needs a reboot to finish an urgent fix"
    report(c, nas, local=WED_0305, reboot_required=False)  # rebooted by the owner
    assert "patch-reboot:nas" not in c.app.state.alerter.problems()


def test_the_weekly_run_takes_urgent_fixes_on_its_own_day(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    c.put("/api/patching/nas", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    c.app.state.urgent.exploited = {"CVE-2027-0001"}
    ups = pending(("openssl", ["CVE-2027-0001"]))
    tue = {"weekday": 1, "time": "03:05", "date": "2027-01-12"}
    run = report(c, nas, local=tue, updates=ups)["patch"]
    assert run["level"] == "full" and "packages" not in run
    report(c, nas, local=tue, patch={"run": run["run"], "state": "ok", "summary": "ok"})
    assert "patch" not in report(c, nas, local={**tue, "time": "04:00"}, updates=ups)


def test_a_proxmox_server_is_never_patched_a_package_at_a_time(env):
    c, csrf, rt, clock = env
    pve = enroll(c, csrf, "pve")
    guests = {"proxmox": {"guests": [], "backups": []}}
    report(c, pve, local={**WED_0305, "time": "01:00"}, **guests)
    assert c.put("/api/patching/pve", headers=csrf,
                 json={**PLAN, "backup": "pve:guests"}).status_code == 200
    c.app.state.urgent.exploited = {"CVE-2027-0001"}
    ups = pending(("pve-kernel", ["CVE-2027-0001"]))
    assert "patch" not in report(c, pve, local=WED_0305, updates=ups, **guests)
    p = c.app.state.alerter.problems()["patch-urgent:pve"]
    assert "only updated as a whole" in p.detail and "Patch now" in p.detail


def test_base_score_reads_the_publisher_and_cisas_enrichment():
    assert base_score({"containers": {"cna": {"metrics": [{"cvssV3_1": {"baseScore": 7.5}}]},
                                      "adp": [{"metrics": [{"cvssV4_0": {"baseScore": 9.3}}]},
                                              {"metrics": [{"other": {"type": "ssvc"}}]}]}}) == 9.3
    assert base_score({"containers": {"cna": {}}}) is None
    assert base_score({"containers": {"cna": {"metrics": [
        {"cvssV3_1": {"baseScore": "x"}}]}}}) is None


def test_lookups_ask_only_about_pending_cves_and_keep_what_they_knew(tmp_path, clock):
    import sqlite3

    from argus.settings import Settings, load_key

    db = sqlite3.connect(tmp_path / "u.db")
    db.row_factory = sqlite3.Row
    settings = Settings(db, load_key(tmp_path / "k"))
    asked, down = [], {"kev": False}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        asked.append(url)
        if url == KEV_URL:
            if down["kev"]:
                return httpx.Response(503)
            return httpx.Response(200, json={"vulnerabilities": [
                {"cveID": "CVE-2027-0001"}, {"cveID": "not-a-cve"}]})
        if url == CVE_URL + "CVE-2027-0003":
            return httpx.Response(200, json={"containers": {"cna": {"metrics": [
                {"cvssV3_1": {"baseScore": 9.8}}]}}})
        return httpx.Response(404)

    wanted = {"CVE-2027-0003", "CVE-2027-0005", "; rm -rf /"}

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            u = Urgent(db, settings, client, clock, lambda: wanted)
            await u.refresh()
            assert u.exploited == {"CVE-2027-0001"} and u.error == ""
            assert u.why("CVE-2027-0001") == "exploited" and u.why("CVE-2027-0003") == "critical"
            assert u.why("CVE-2027-0005") == ""  # no record yet: not urgent, asked again later
            assert sorted(a for a in asked if a.startswith(CVE_URL)) == [
                CVE_URL + "CVE-2027-0003", CVE_URL + "CVE-2027-0005"]
            # Nothing is due for hours: no request at all.
            asked.clear()
            clock.now += 3600
            await u.refresh()
            assert asked == []
            # The list is down later: what Argus knew stays, and it says so.
            down["kev"] = True
            clock.now += 7 * 3600
            await u.refresh()
            assert u.exploited == {"CVE-2027-0001"} and "CISA" in u.error
            # A restart keeps both.
            again = Urgent(db, settings, client, clock, lambda: wanted)
            assert again.exploited == {"CVE-2027-0001"}
            assert again.why("CVE-2027-0003") == "critical"

    asyncio.run(go())


# --- apps: a critical fix that will not arrive on its own ---

def app_state(alerts, updates):
    return {"alerts": alerts, "updates": [
        {"number": n, "title": t, "url": f"https://github.com/ann/shop/pull/{n}",
         "ci": ci, "waits_for_you": waits} for n, t, ci, waits in updates]}


def alert(package, severity, cve=""):
    return {"package": package, "severity": severity, "cve": cve,
            "summary": f"{package} lets strangers in", "url": "https://github.com/a"}


def test_an_apps_critical_fix_that_is_a_major_update_reaches_the_owner(env):
    from types import SimpleNamespace

    from argus.github import stuck_fixes

    c, csrf, rt, clock = env
    state = app_state(
        [alert("next", "critical", "CVE-2027-0100"), alert("lodash", "high", "CVE-2027-0101"),
         alert("express", "critical"), alert("pg", "critical"), alert("ws", "high", "CVE-2027-9")],
        [(12, "Bump next from 14.2.3 to 15.0.1", "success", True),      # a major: waits
         (13, "Bump lodash from 4.17.20 to 5.0.0", "success", True),    # high: not urgent
         (14, "Bump express from 4.19.0 to 4.19.2", "running", False),  # merges by itself
         (15, "Bump pg from 8.11.0 to 8.11.5", "failure", False),       # stuck: failing
         (16, "Bump ws from 8.17.0 to 9.0.0", "success", True),         # exploited below
         (17, "Bump next-auth from 4.0.0 to 5.0.0", "success", True)])  # not "next"
    got = stuck_fixes(state, lambda cve: cve == "CVE-2027-9")
    assert [(f["package"], f["hot"], f["why"], f["pr"]) for f in got] == [
        ("next", "critical", "waits", 12), ("pg", "critical", "failing", 15),
        ("ws", "exploited", "waits", 16)]
    assert stuck_fixes({"alerts": None, "updates": []}) == []  # alerts not readable
    # The alerter says it, once per app and package.
    al = c.app.state.alerter
    al.gh = SimpleNamespace(repos={"shop": state})
    c.app.state.urgent.exploited = {"CVE-2027-9"}
    p = al.problems()
    assert p["app-fix:shop:next"].title == "shop has a security fix waiting for you"
    assert p["app-fix:shop:next"].detail == (
        "next has a critical flaw (CVE-2027-0100). Its fix, PR #12, is a major update, "
        "which Argus leaves to you.")
    assert "fails its checks" in p["app-fix:shop:pg"].detail
    assert "being exploited" in p["app-fix:shop:ws"].detail
    assert "app-fix:shop:lodash" not in p and "app-fix:shop:express" not in p
    # The owner can turn the rule off.
    assert c.put("/api/alerts", headers=csrf, json={"rules": {"apps": False}}).status_code == 200
    assert not any(k.startswith("app-fix") for k in al.problems())


def test_the_owner_sets_what_goes_in_at_once_and_what_alerts(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    c.put("/api/patching/nas", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    u, al = c.app.state.urgent, c.app.state.alerter
    u.scores = {"CVE-2027-0002": (7.8, clock.now), "CVE-2027-0003": (5.0, clock.now)}
    ups = pending(("vim", ["CVE-2027-0002"]), ("tar", ["CVE-2027-0003"]))
    # The defaults: 7.8 neither goes in at once nor alerts.
    assert c.get("/api/alerts").json()["security"] == {
        "install_exploited": True, "install_min": 9.0, "alert_min": 9.0}
    assert "patch" not in report(c, nas, local=WED_0305, updates=ups)
    assert "patch-cve:nas" not in al.problems()

    # Alert from 7.0: it waits for Tuesday's window, and says so.
    r = c.put("/api/alerts", headers=csrf, json={"security": {
        "install_exploited": True, "install_min": 9.0, "alert_min": 7.0}})
    assert r.status_code == 200 and r.json()["security"]["alert_min"] == 7.0
    p = al.problems()["patch-cve:nas"]
    assert p.title == "nas has a security fix waiting"
    assert p.detail == ("vim (CVE-2027-0002 scores 7.8 (high)). It goes in with the weekly "
                        "run on Tuesday at 03:00, or press Patch now.")

    # Install from 7.0 too: it goes in tonight, so nothing to alert about.
    c.put("/api/alerts", headers=csrf, json={"security": {
        "install_exploited": True, "install_min": 7.0, "alert_min": 7.0}})
    assert "patch-cve:nas" not in al.problems()
    run = report(c, nas, local={**WED_0305, "date": "2027-01-20"}, updates=ups)["patch"]
    assert run["level"] == "urgent" and run["packages"] == ["vim"]


def test_exploited_can_wait_for_the_window_but_still_alerts(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    u, al = c.app.state.urgent, c.app.state.alerter
    u.exploited = {"CVE-2027-0001"}
    ups = pending(("openssl", ["CVE-2027-0001"]))
    report(c, nas, local=WED_0305, updates=ups)
    # Argus does not patch nas: the owner hears about it.
    p = al.problems()["patch-cve:nas"]
    assert p.detail.startswith("openssl (CVE-2027-0001 is being exploited). Argus does not "
                               "patch this host")
    c.put("/api/patching/nas", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    assert "patch-cve:nas" not in al.problems()  # it goes in tonight
    c.put("/api/alerts", headers=csrf, json={"security": {
        "install_exploited": False, "install_min": None, "alert_min": 9.0}})
    assert "patch" not in report(c, nas, local=WED_0305, updates=ups)
    assert "weekly run on Tuesday" in al.problems()["patch-cve:nas"].detail
    c.put("/api/alerts", headers=csrf, json={"security": {
        "install_exploited": False, "install_min": None, "alert_min": None}})
    assert "patch-cve:nas" not in al.problems()
