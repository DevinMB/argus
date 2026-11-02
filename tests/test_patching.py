import logging
import secrets
import textwrap

import pytest
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus.app import create_app
from argus.runtime import Runtime

CONFIG = """
jobs:
  - {name: nas-backup, max_age: 691200}
  - {name: nas-restore-test, max_age: 3024000}
"""
TUE_0305 = {"weekday": 1, "time": "03:05", "date": "2027-01-12"}
PLAN = {"enabled": True, "day": 1, "time": "03:00", "level": "full", "reboot": True}


@pytest.fixture
def env(tmp_path, prober, clock, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    (tmp_path / "argus.yml").write_text(textwrap.dedent(CONFIG))
    rt = Runtime(tmp_path / "argus.yml", tmp_path / "argus.db", prober=prober, clock=clock)
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c, {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}, rt, clock


def enroll(c, csrf, name, read_only=False):
    code = c.post("/api/companions/codes", headers=csrf, json={
        "url": "https://argus.lan", "name": name, "read_only": read_only}).json()["code"]
    return c.post("/companion/enroll", json={"code": code, "hostname": name}).json()["token"]


def report(c, tok, **fields):
    return c.post("/companion/report", headers={"Authorization": f"Bearer {tok}"},
                  json={"nonce": secrets.token_urlsafe(16), **fields}).json()


def job_ok(c, rt, clock, name):
    r = c.post(f"/job/{name}", json={"result": "ok", "started": clock.now - 60,
                                     "finished": clock.now})
    assert r.status_code == 204


def test_the_window_grants_one_run_with_what_the_safety_net_allows(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    assert c.put("/api/patching/nas", headers=csrf, json=PLAN).status_code == 200
    assert "patch" not in report(c, nas, local={**TUE_0305, "time": "02:59"})
    run = report(c, nas, local=TUE_0305)["patch"]
    # No safety net: security updates only, and no reboot, whatever the plan asks.
    assert run["level"] == "security" and run["reboot"] is False
    assert "patch" not in report(c, nas, local=TUE_0305,
                                 patch={"run": run["run"], "state": "running"})
    report(c, nas, local=TUE_0305, patch={"run": run["run"], "state": "ok",
                                          "summary": "12 packages"})
    assert "patch" not in report(c, nas, local={**TUE_0305, "time": "04:00"})  # once a window
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert view["runs"][0]["result"] == "ok" and view["safety"]["net"] == "none"


def test_how_far_a_run_goes_follows_the_backups(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    plan = {**PLAN, "backup": "job:nas-backup"}
    c.put("/api/patching/nas", headers=csrf, json=plan)
    job_ok(c, rt, clock, "nas-backup")
    v = c.put("/api/patching/nas", headers=csrf, json=plan).json()
    assert v["safety"]["net"] == "a fresh backup, never restore-tested" and not v["safety"]["full"]
    v = c.put("/api/patching/nas", headers=csrf, json={**plan, "accept_untested": True}).json()
    assert v["safety"]["full"]
    job_ok(c, rt, clock, "nas-restore-test")
    v = c.put("/api/patching/nas", headers=csrf,
              json={**plan, "restore": "job:nas-restore-test"}).json()
    assert v["safety"]["net"] == "a fresh backup with a recent restore test"
    run = report(c, nas, local=TUE_0305)["patch"]
    assert run["level"] == "full" and run["reboot"] is True
    # A ZFS root is a safety net of its own.
    zfs = enroll(c, csrf, "zfs-box")
    c.put("/api/patching/zfs-box", headers=csrf, json=PLAN)
    report(c, nas, local=TUE_0305, patch={"run": run["run"], "state": "ok"})
    zrun = report(c, zfs, local=TUE_0305, snapshot="zfs")["patch"]
    assert zrun["level"] == "full" and zrun["snapshot"] is True


def test_a_stale_backup_skips_the_window_and_pages(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    c.put("/api/patching/nas", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    clock.now += 9 * 86400
    assert "patch" not in report(c, nas, local=TUE_0305)
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert view["skipped"] == "its backup (job:nas-backup) is 9 days old"
    titles = [p.title for p in c.app.state.alerter.problems().values()]
    assert "nas was not patched" in titles


def test_one_host_at_a_time_and_never_on_sunday(env):
    c, csrf, rt, clock = env
    a, b = enroll(c, csrf, "a"), enroll(c, csrf, "b")
    for h in ("a", "b"):
        c.put(f"/api/patching/{h}", headers=csrf, json=PLAN)
    run = report(c, a, local=TUE_0305)["patch"]
    assert "patch" not in report(c, b, local=TUE_0305)  # waits its turn
    report(c, a, local=TUE_0305, patch={"run": run["run"], "state": "ok"})
    assert "patch" in report(c, b, local=TUE_0305)
    r = c.put("/api/patching/a", headers=csrf, json={**PLAN, "day": 6})
    assert r.status_code == 422 and "backups" in r.json()["detail"]
    # Patch now on a Sunday is refused too, with the reason.
    c.post("/api/patching/a/now", headers=csrf)
    assert "patch" not in report(c, a, local={"weekday": 6, "time": "12:00",
                                              "date": "2027-01-17"})
    assert "Sunday" in c.get("/api/companions").json()["companions"][0]["patching"]["skipped"]


def test_patch_now_stuck_runs_and_monitoring_only_hosts(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    ro = enroll(c, csrf, "ro", read_only=True)
    assert c.post("/api/patching/ro/now", headers=csrf).status_code == 409
    assert "patch" not in report(c, ro, local=TUE_0305)
    c.post("/api/patching/nas/now", headers=csrf)  # no plan needed for a one-off
    run = report(c, nas, local={"weekday": 3, "time": "15:00", "date": "2027-01-14"})["patch"]
    assert run["level"] == "security"
    clock.now += 2 * 3600 + 1
    report(c, nas, local={"weekday": 3, "time": "17:01", "date": "2027-01-14"})
    view = c.get("/api/companions").json()["companions"]
    nas_view = next(v for v in view if v["name"] == "nas")["patching"]
    assert nas_view["runs"][0]["result"] == "failed"
    assert "patching nas failed" in [p.title for p in c.app.state.alerter.problems().values()]
    assert c.put("/api/patching/nas", json=PLAN).status_code == 403  # an admin write


def test_a_host_patched_by_something_else(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    r = c.put("/api/patching/nas", headers=csrf, json={**PLAN, "elsewhere": True})
    assert r.status_code == 422 and "would fight" in r.json()["detail"]
    v = c.put("/api/patching/nas", headers=csrf, json={
        **PLAN, "enabled": False, "elsewhere": True, "elsewhere_job": "job:nas-backup"}).json()
    assert v["elsewhere"]["status"] == "never"
    job_ok(c, rt, clock, "nas-backup")
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert view["elsewhere"]["status"] == "ok" and view["elsewhere"]["at"] == clock.now
    # Argus never patches it, in its window or on request.
    assert "patch" not in report(c, nas, local=TUE_0305)
    assert c.post("/api/patching/nas/now", headers=csrf).status_code == 409
    # And the checklist counts it as patched.
    steps = {s["key"]: s for s in c.get("/api/start").json()["steps"]}
    assert steps["patching"]["done"]


def test_a_host_left_without_a_backup_on_purpose_is_never_paged_about_one(env):
    # v2's side of the 2026-11-02 false page: Argus only watches a backup the
    # owner names for a host, so a host left out on purpose has nothing to go stale.
    c, csrf, rt, clock = env
    lab = enroll(c, csrf, "rover-lab")
    c.put("/api/patching/rover-lab", headers=csrf, json=PLAN)
    job_ok(c, rt, clock, "nas-backup")  # someone else's backup, weeks ago
    clock.now += 30 * 86400
    run = report(c, lab, local=TUE_0305)["patch"]
    assert run["level"] == "security"  # no safety net: careful, but patched
    report(c, lab, local=TUE_0305, patch={"run": run["run"], "state": "ok"})
    assert c.app.state.alerter.problems() == {}


def test_a_removed_hosts_plan_is_not_inherited_by_a_new_host_with_its_name(env):
    # The VMID-reuse shape: a new machine under an old name must start clean.
    c, csrf, rt, clock = env
    old = enroll(c, csrf, "lab")
    c.put("/api/patching/lab", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    run = report(c, old, local=TUE_0305)["patch"]
    report(c, old, local=TUE_0305, patch={"run": run["run"], "state": "failed",
                                          "summary": "dpkg broke"})
    c.post("/api/patching/lab/now", headers=csrf)
    cid = c.get("/api/companions").json()["companions"][0]["id"]
    assert c.delete(f"/api/companions/{cid}", headers=csrf).status_code == 200
    enroll(c, csrf, "lab")
    host = c.get("/api/companions").json()["companions"][0]
    assert host["name"] == "lab"
    assert host["patching"]["plan"]["enabled"] is False
    assert host["patching"]["plan"]["backup"] == ""
    assert host["patching"]["runs"] == []
    assert c.app.state.alerter.problems() == {}  # the old host's failed run went with it
