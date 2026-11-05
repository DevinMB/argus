import { describe, expect, it } from "vitest";
import { acrossSites, bytes, diskTone, hostSentence, sortContainers, errorText, pagingSentence, watcherSentence, jobLine, took, ago, agoText, filterChecks, groupChecks, headline, isPublic, joinList, ringOrder, safeHref, siteName, siteNote, summarize, jobResult, lifecycle, lifecycleHeadline, nextRun, recentMerges, scheduleDay } from "./lib.js";

const c = (name, status, extra = {}) => ({
  name, status, site: "B", group: "apps", target: `http://${name}/`, ...extra,
});

describe("summarize", () => {
  it("counts each status and treats anything else as unknown", () => {
    expect(summarize([c("a", "up"), c("b", "down"), c("c", "weird")])).toEqual({
      total: 3, up: 1, failing: 0, down: 1, unknown: 1, paused: 0,
    });
  });
});

describe("groupChecks", () => {
  it("keeps the config's group order and puts the worst check first in each", () => {
    const out = groupChecks([
      c("b", "up", { group: "z" }), c("a", "up"), c("c", "down"), c("d", "failing"),
    ]);
    expect(out.map(([g, l]) => [g, l.map((x) => x.name)])).toEqual([
      ["z", ["b"]], ["apps", ["c", "d", "a"]],
    ]);
  });
});

describe("ringOrder", () => {
  it("keeps each site in one arc, in the configured order", () => {
    const checks = [c("x", "up", { site: "A" }), c("y", "up"), c("w", "up", { site: "A" })];
    expect(ringOrder(checks, ["B", "A"]).map((x) => x.name)).toEqual(["y", "w", "x"]);
    expect(ringOrder(checks, ["A", "B"]).map((x) => x.name)).toEqual(["w", "x", "y"]);
  });
});

describe("isPublic", () => {
  it.each([
    [{ kind: "http", target: "https://status.example.com/" }, true],
    [{ kind: "http", target: "http://192.0.2.5:8123/" }, false],
    [{ kind: "http", target: "http://homepage:3000/" }, false],
    [{ kind: "tcp", target: "192.0.2.250:9092" }, false],
  ])("%o -> %s", (check, want) => expect(isPublic(check)).toBe(want));
});

describe("filterChecks", () => {
  const all = [c("vault", "down"), c("kafka", "failing", { site: "A" }), c("web", "up")];
  it("problems means down or failing", () => {
    expect(filterChecks(all, { status: "problems" }).map((x) => x.name)).toEqual(["vault", "kafka"]);
  });
  it("filters by site and free text over name, target and group", () => {
    expect(filterChecks(all, { site: "A" }).map((x) => x.name)).toEqual(["kafka"]);
    expect(filterChecks(all, { q: "WEB/" }).map((x) => x.name)).toEqual(["web"]);
  });
  it("matches the tags a tile shows", () => {
    const tagged = [
      c("shop", "up", { kind: "http", target: "https://shop.example.com/", severity: "critical" }),
      c("lan", "up", { kind: "tcp", target: "192.168.1.2:5432", site: "A", severity: "warning" }),
    ];
    const names = (q) => filterChecks(tagged, { q, sites: { A: "Remote", B: "Home" } }).map((x) => x.name);
    expect(names("public")).toEqual(["shop"]);
    expect(names("pages")).toEqual(["shop"]);
    expect(names("HOME")).toEqual(["shop"]);
    expect(names("tcp")).toEqual(["lan"]);
    expect(names("site a")).toEqual(["lan"]);
    expect(names("remote")).toEqual(["lan"]);
  });
});

describe("ago", () => {
  it.each([[null, "never"], [-3, "now"], [42, "42s"], [600, "10m"], [7300, "2h"], [200000, "2d"]])(
    "%s -> %s", (s, want) => expect(ago(s)).toBe(want),
  );
});

describe("agoText", () => {
  it.each([[1, "just now"], [null, "never"], [600, "10m ago"]])(
    "%s -> %s", (s, want) => expect(agoText(s)).toBe(want),
  );
});

describe("joinList", () => {
  it.each([[[], ""], [["a"], "a"], [["a", "b"], "a and b"], [["a", "b", "c"], "a, b and c"]])(
    "%o -> %s", (items, want) => expect(joinList(items)).toBe(want),
  );
});

describe("headline and siteNote", () => {
  const s = (o) => ({ total: 3, up: 0, failing: 0, down: 0, unknown: 0, ...o });
  it("never says all up while checks are waiting", () => {
    expect(headline(s({ up: 2, unknown: 1 }))).toBe("Starting up.");
    expect(siteNote(s({ up: 2, unknown: 1 }))).toBe("1 waiting");
  });
  it("worst state wins", () => {
    expect(headline(s({ down: 1, failing: 1, unknown: 1 }))).toBe("Something is down.");
    expect(siteNote(s({ failing: 2, unknown: 1 }))).toBe("2 failing");
    expect(headline(s({ up: 3 }))).toBe("All quiet.");
    expect(siteNote(s({ up: 3 }))).toBe("all up");
  });
  it("a paused check is quiet: no problem, but said", () => {
    expect(headline(s({ up: 2, paused: 1 }))).toBe("All quiet.");
    expect(siteNote(s({ up: 2, paused: 1 }))).toBe("1 paused");
  });
});

describe("siteName", () => {
  it("uses the configured name, falling back to the id", () => {
    expect(siteName({ B: "Home" }, "B")).toBe("Home");
    expect(siteName({ B: "Home" }, "A")).toBe("A");
    expect(siteName(undefined, "A")).toBe("A");
  });
});

describe("life cycle", () => {
  const step = (key, status, detail = `${key} ${status}`) => ({ key, status, title: key, detail });
  const DONE = ["updates", "merged", "built", "bumped", "deployed", "healthy"].map((k) => step(k, "done"));
  const repo = (extra = {}) => ({
    name: "shop", updates: [], merged: [], alerts: [], stack: { host: "box" },
    main: { sha: "abc1234", status: "success", title: "Bump httpx" }, track: DONE, ...extra,
  });
  const pr = (over) => ({ number: 1, title: "Bump vite", url: "https://x", ci: "running", waits_for_you: false, ...over });
  const swap = (key, status, detail) => DONE.map((s) => (s.key === key ? step(key, status, detail) : s));

  it("is up to date when every step is done, and says what runs where", () => {
    const s = lifecycle(repo());
    expect(s).toMatchObject({ column: "live", tone: "up", label: "Up to date", you: false });
    expect(s.sentence).toBe("Live on box. Last change: Bump httpx.");
  });
  it("a paused check is neither down nor needs you", () => {
    const s = lifecycle(repo({ track: swap("healthy", "paused", "web is paused: off for now") }));
    expect(s).toMatchObject({ column: "live", tone: "paused", label: "Paused", you: false });
    expect(s.sentence).toBe("Live on box. web is paused: off for now, so it does not page.");
  });
  it("says so when there is no health check, and when it ships inside another app", () => {
    expect(lifecycle(repo({ track: swap("healthy", "not_wired") })).sentence).toMatch(/no health check/);
    const lib = lifecycle(repo({ track: DONE.slice(0, 3), stack: null }));
    expect(lib.label).toBe("Up to date");
    expect(lib.sentence).toMatch(/ships inside another app/);
    // Found by "Connect GitHub": its deploy is unknown, so no claim about it.
    const found = lifecycle(repo({ track: DONE.slice(0, 3), stack: null, configured: false }));
    expect(found.sentence).toMatch(/does not follow its deploys/);
    expect(lifecycle(repo({ main: null, branch: "trunk" })).sentence).toMatch(/No CI runs on trunk/);
  });
  it("puts each open update where it is: testing, merging, or waiting for you", () => {
    expect(lifecycle(repo({ updates: [pr()] }))).toMatchObject({ column: "testing", tone: "accent", you: false });
    expect(lifecycle(repo({ updates: [pr({ ci: "success" })] }))).toMatchObject({ column: "merging", you: false });
    expect(lifecycle(repo({ updates: [pr({ waits_for_you: true, ci: "success" })] })))
      .toMatchObject({ column: "merging", label: "Waits for you", you: true });
    const red = lifecycle(repo({ updates: [pr({ ci: "failure" }), pr({ number: 2, ci: "failure" })] }));
    expect(red).toMatchObject({ column: "testing", tone: "down", you: true });
    expect(red.sentence).toMatch(/and 1 more/);
  });
  it("follows a merged change through build and deploy", () => {
    expect(lifecycle(repo({ track: swap("built", "running") }))).toMatchObject({ column: "building", label: "Building" });
    expect(lifecycle(repo({ track: swap("deployed", "running") }))).toMatchObject({ column: "deploying", you: false });
    expect(lifecycle(repo({ track: swap("built", "failed", "Failed at Tests") })))
      .toMatchObject({ column: "building", tone: "down", sentence: "Failed at Tests", you: true });
    expect(lifecycle(repo({ track: swap("healthy", "failed") }))).toMatchObject({ column: "live", label: "Down", you: true });
  });
  it("a broken step outranks a waiting major, which outranks one in CI", () => {
    const both = repo({ updates: [pr({ waits_for_you: true }), pr()], track: swap("bumped", "failed") });
    expect(lifecycle(both).label).toBe("Deploy stuck");
    expect(lifecycle(repo({ updates: [pr(), pr({ waits_for_you: true })] })).label).toBe("Waits for you");
  });
  it("says when it went live, and flags a failing Dependabot", () => {
    const live = DONE.map((s) => (s.key === "deployed" ? { ...s, at: 123 } : s));
    expect(lifecycle(repo({ track: live })).deployedAt).toBe(123);
    const dep = lifecycle(repo({ track: swap("updates", "failed", "Dependabot's last look failed: pip") }));
    expect(dep).toMatchObject({ label: "Dependabot failing", you: true, column: "testing" });
  });
  it("leads the page with one sentence", () => {
    const up = { you: false, tone: "up" };
    expect(lifecycleHeadline([up, up])).toBe("All 2 apps up to date.");
    expect(lifecycleHeadline([up, { you: false, tone: "accent" }])).toBe("1 update on the way, nothing needs you.");
    expect(lifecycleHeadline([{ you: true }, { you: true }, up])).toBe("2 apps need you.");
  });
  it("lists merges across repos, newest first", () => {
    const rs = [{ name: "a", merged: [{ number: 1, merged_at: 5 }] },
                { name: "b", merged: [{ number: 2, merged_at: 9 }, { number: 3, merged_at: 1 }] }];
    expect(recentMerges(rs, 2).map((m) => `${m.repo}#${m.number}`)).toEqual(["b#2", "a#1"]);
  });
  it("draws only https links", () => {
    expect(safeHref("https://github.com/x")).toBe("https://github.com/x");
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref(null)).toBeUndefined();
  });
});

describe("jobs", () => {
  it("says how long a run took", () => {
    expect(took(0, 42)).toBe("42s");
    expect(took(0, 754)).toBe("12m");
    expect(took(0, 3840)).toBe("1h 4m");
    expect(took(null, 5)).toBe("–");
  });
  it("describes a job's state in one line", () => {
    expect(jobLine({ last: null, schedule: "Mondays 03:00" }, 0)).toBe("first run Mondays 03:00");
    const last = { finished: 1000, result: "ok" };
    expect(jobLine({ last, status: "ok", max_age: 60 }, 1240)).toBe("passed 4m ago");
    expect(jobLine({ last: { ...last, result: "failed" }, status: "failed" }, 1000)).toBe("failed just now");
    expect(jobLine({ last, status: "late", max_age: 691200 }, 1000 + 9 * 86400))
      .toBe("last ran 9d ago, past its 8d limit");
  });
  it("finds the next run of a weekly schedule, skipping one already past", () => {
    // Thursday 2026-10-29 12:00 local.
    const thu = new Date(2026, 9, 29, 12, 0).getTime() / 1000;
    expect(nextRun("Mondays 03:00", thu)).toMatch(/Nov 2, 03:00$/);
    expect(nextRun("Thursdays 03:00", thu)).toMatch(/Nov 5, 03:00$/);
    expect(nextRun("every 5 minutes", thu)).toBeNull();
    expect(scheduleDay("Tuesdays 03:00")).toEqual({ day: 2, time: "03:00" });
    expect(scheduleDay("after each merge")).toBeNull();
  });
  it("says how the last run went, counting what it covered", () => {
    expect(jobResult({ last: null, schedule: "Mondays 03:00" }, 0)).toBe("Has not run yet. First run: Mondays 03:00.");
    const items = [{ result: "ok" }, { result: "ok" }, { result: "skipped" }];
    const job = { status: "ok", last: { result: "ok", started: 0, finished: 754, summary: "2 patched", items } };
    expect(jobResult(job, 754 + 3600)).toBe("Passed 1h ago, took 12m: 2 of 3 done, 1 skipped.");
    const bare = { status: "late", max_age: 86400, last: { ...job.last, result: "failed", items: [] } };
    expect(jobResult(bare, 754 + 2 * 86400)).toBe("Failed 2d ago, and is overdue (limit 1d), took 12m: 2 patched.");
  });
});

describe("sites", () => {
  it("names a site by its configured name, else its id", () => {
    expect(siteName({ home: "Home" }, "home")).toBe("Home");
    expect(siteName({}, "cabin")).toBe("cabin");
  });
  it("says how many sites only when there is more than one", () => {
    expect(acrossSites(1)).toBe("");
    expect(acrossSites(2)).toBe(" across both sites");
    expect(acrossSites(5)).toBe(" across 5 sites");
  });
  it("orders any number of sites, unknown ones last", () => {
    const checks = [c("z", "up", { site: "x" }), c("a", "up", { site: "c" }), c("b", "up", { site: "a" })];
    expect(ringOrder(checks, ["a", "b", "c"]).map((x) => x.site)).toEqual(["a", "c", "x"]);
  });
});

describe("alerts text", () => {
  it("turns validation errors into words", () => {
    expect(errorText("sign in first")).toBe("sign in first");
    expect(errorText([{ loc: ["body", "chat_id"], msg: "String should match pattern" }]))
      .toMatch(/^A chat ID is a number/);
    expect(errorText([{ loc: ["body", "other"], msg: "Field required" }])).toBe("Field required");
    expect(errorText(undefined)).toBe("");
  });
  it("says who pages, and when nobody does", () => {
    expect(pagingSentence({ sender: "argus", ready: true }, 3)).toMatch(/Argus sends it to Telegram/);
    expect(pagingSentence({ sender: "argus", ready: false }, 1)).toMatch(/^1 check is marked pages, but nothing sends/);
    expect(pagingSentence({ sender: "prometheus", ready: true }, 2)).toMatch(/Prometheus and Alertmanager/);
  });
  it("says plainly when nothing watches Argus", () => {
    expect(watcherSentence({ watched: false, sender: "argus" })).toMatch(/^Nothing watches Argus/);
    expect(watcherSentence({ watched: true })).toMatch(/outside service/);
    expect(watcherSentence({ watched: false, sender: "prometheus" })).toMatch(/ArgusDown/);
  });
});

describe("hosts", () => {
  it("formats sizes", () => {
    expect(bytes(512)).toBe("512 B");
    expect(bytes(1.8 * 1024 ** 3)).toBe("1.8 GB");
    expect(bytes(29895020544)).toBe("28 GB");
  });

  it("tones disks like the server pages them", () => {
    expect(diskTone({ total: 100, used: 50 })).toBe("up");
    expect(diskTone({ total: 100, used: 90 })).toBe("failing");
    expect(diskTone({ total: 100, used: 96 })).toBe("down");
  });

  it("puts troubled containers first", () => {
    const list = [{ name: "b", state: "running" }, { name: "a", state: "running", health: "unhealthy" },
                  { name: "c", state: "exited" }];
    expect(sortContainers(list).map((c) => c.name)).toEqual(["a", "c", "b"]);
  });

  it("sums a host up in one sentence", () => {
    expect(hostSentence({ status: "waiting" })).toBe("Enrolled; waiting for its first report.");
    expect(hostSentence({ status: "ok", report: {
      containers: [{ name: "a", state: "running" }, { name: "b", state: "exited" }],
      disks: [{ total: 100, used: 40 }, { total: 100, used: 91 }],
      failed_units: [], updates: { pending: 3, security: 1 }, reboot_required: true,
    } })).toBe("2 containers, 1 not running well, 2 disks, fullest 91% used, 3 updates waiting (1 security), needs a reboot.");
    expect(hostSentence({ status: "ok", report: { containers: null, disks: [{ total: 10, used: 1 }] } }))
      .toBe("Disk 10% used.");
  });
});
