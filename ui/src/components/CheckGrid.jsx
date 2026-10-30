import { ago, groupChecks, isPublic, ms, siteName, summarize } from "../lib.js";

const LABEL = { up: "Up", failing: "Failing", down: "Down", unknown: "Waiting" };

// A check with something to say gets the full tile: target, error, tags, timing.
function Cell({ c, now, sites, onPick }) {
  return (
    <button className={`cell cell-${c.status}`} onClick={() => onPick(c.name)}>
      <div className="cell-top">
        <span className={`dot dot-${c.status}`} aria-hidden="true" />
        <span className="cell-name">{c.name}</span>
        <span className={`cell-status st-${c.status}`}>{LABEL[c.status] ?? c.status}</span>
      </div>
      <div className="cell-target" title={c.target}>{c.target}</div>
      {c.last_error && <div className="cell-error">{c.last_error}</div>}
      <div className="chips">
        <span className="chip">{c.kind}</span>
        {c.severity === "critical" && <span className="chip chip-critical">pages</span>}
        {isPublic(c) && <span className="chip chip-public" title="Reached from the internet">public</span>}
        {Object.keys(sites ?? {}).length > 1 && <span className="chip">{siteName(sites, c.site)}</span>}
      </div>
      <div className="cell-foot">
        <span>{c.last_change ? `since ${ago(now - c.last_change)}` : "no result yet"}</span>
        <span>{ms(c.last_duration)}</span>
        {c.consecutive_failures > 0 && <span className="warn">{c.consecutive_failures} failed</span>}
      </div>
    </button>
  );
}

// A healthy check is one line: what it is and that it is fine. The drawer has the rest.
function Row({ c, onPick }) {
  return (
    <button className="crow" onClick={() => onPick(c.name)} title={c.description || c.target}>
      <span className="dot dot-up" aria-hidden="true" />
      <span className="crow-name">{c.name}</span>
      {c.severity === "critical" && <span className="crow-pages" title="Pages if down">pages</span>}
      <span className="crow-target">{c.target.replace(/^https?:\/\//, "")}</span>
    </button>
  );
}

export default function CheckGrid({ checks, now, sites, groups = {}, onPick }) {
  if (!checks.length) return <p className="empty">No checks match.</p>;
  return groupChecks(checks).map(([group, list]) => {
    const s = summarize(list);
    const bad = list.filter((c) => c.status !== "up");
    const good = list.filter((c) => c.status === "up");
    return (
      <section key={group} className="group">
        <h2 className="eyebrow">
          {group} <span className="count">{list.length}</span>
          <span className={`group-state st-${s.down ? "down" : s.failing ? "failing" : "up"}`}>
            {s.up === s.total ? "all up" : `${s.total - s.up} not up`}
          </span>
        </h2>
        {groups[group] && <p className="group-hint">{groups[group][0].toUpperCase() + groups[group].slice(1)}.</p>}
        {bad.length > 0 && (
          <div className="grid">
            {bad.map((c) => <Cell key={c.name} c={c} now={now} sites={sites} onPick={onPick} />)}
          </div>
        )}
        {good.length > 0 && (
          <div className="crows">
            {good.map((c) => <Row key={c.name} c={c} onPick={onPick} />)}
          </div>
        )}
      </section>
    );
  });
}
