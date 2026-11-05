import { useEffect, useRef, useState } from "react";
import { send } from "../api.js";

// The first-run checklist: one next step at a time, each with why it matters.
export default function GetStarted({ start, session, onWatch, onAddCheck, reload }) {
  const [picked, setPicked] = useState(null);
  if (!start || start.hidden || start.done === start.steps.length) return null;
  // The next undone step is open; any other opens when its title is pressed.
  const next = start.steps.find((s) => s.key === picked && !s.done) ?? start.steps.find((s) => !s.done);
  const hide = () => send("PUT", "/api/start", { hidden: true }, session?.csrf).then(reload, () => {});
  return (
    <section className="start panel" aria-label="Get started">
      <div className="start-head">
        <h2>Get started</h2>
        <span className="muted small">{start.done} of {start.steps.length} done</span>
        {session?.signed_in && <button className="link-button small" onClick={hide}>Hide</button>}
      </div>
      <span className="bar" aria-hidden="true">
        <span className="fill-up" style={{ width: `${(100 * start.done) / start.steps.length}%` }} />
      </span>
      <ol className="start-steps">
        {start.steps.map((s) => (
          <li key={s.key} className={s.done ? "start-done" : s === next ? "start-next" : ""}>
            <span className={`dot dot-${s.done ? "up" : s === next ? "accent" : "unknown"}`} />
            <div>
              <button className="link-button start-title" onClick={() => setPicked(s.key)} disabled={s.done}
                      aria-expanded={s === next}>
                <strong>{s.title}</strong>
              </button>
              {s === next && (
                <>
                  <p className="muted">{s.why}</p>
                  {s.key === "checks" && start.suggestions.length > 0 && (
                    <ul className="host-list suggest">
                      {start.suggestions.slice(0, 6).map((g) => (
                        <li key={`${g.host}-${g.port}`}>
                          <span className="dot dot-unknown" />
                          <span className="host-item">{g.container} <span className="muted mono">:{g.port} on {g.host}</span></span>
                          <button className="btn" onClick={() => onWatch(g.check)}>Watch</button>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="actions">
                    {s.key === "checks"
                      ? <button className="btn btn-primary" onClick={onAddCheck}>{start.suggestions.length ? "Add something else" : s.action}</button>
                      : <a className="btn btn-primary" href={s.href}>{s.action}</a>}
                  </div>
                </>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

// The checklist on every other page while setup is unfinished: where you are,
// what is next, and a way back. When a step gets done here, it says so.
export function SetupBar({ start, session, view }) {
  const before = useRef(null);
  const [justDone, setJustDone] = useState(null);
  useEffect(() => {
    if (!start) return;
    const done = new Set(start.steps.filter((s) => s.done).map((s) => s.key));
    if (before.current) {
      const fresh = start.steps.find((s) => done.has(s.key) && !before.current.has(s.key));
      if (fresh) setJustDone(fresh.title);
    }
    before.current = done;
  }, [start]);
  if (!session?.signed_in || !start || start.hidden || view === "checks") return null;
  const next = start.steps.find((s) => !s.done);
  if (!next && !justDone) return null;
  const here = next && next.href === `#/${view}`;
  return (
    <p className={`banner setup-bar ${justDone ? "setup-bar-done" : ""}`} role="status">
      {justDone && <span><span className="st-up">Done:</span> {justDone}. </span>}
      {next ? (
        <span>
          Setup, {start.done} of {start.steps.length}. {here ? "This page: " : "Next: "}<strong>{next.title}</strong>.{" "}
          {!here && next.href && next.key !== "checks" && <a href={next.href}>{next.action}</a>}
          {(here || next.key === "checks") && <a href="#">Back to the checklist</a>}
        </span>
      ) : <span>Setup is complete. <a href="#">Home</a></span>}
    </p>
  );
}

// "12 of 15 services watched · 4 of 4 hosts reporting · 9 of 10 stacks from git"
export function coverageLine(c) {
  if (!c || !c.hosts) return null;
  const parts = [];
  if (c.services) parts.push(`${c.watched} of ${c.services} published service${c.services === 1 ? "" : "s"} watched`);
  parts.push(`${c.reporting} of ${c.hosts} host${c.hosts === 1 ? "" : "s"} reporting`);
  if (c.stacks) parts.push(`${c.deployed} of ${c.stacks} stack${c.stacks === 1 ? "" : "s"} deployed from git`);
  return parts.join(" · ");
}
