import { useEffect, useState } from "react";
import { getJob } from "../api.js";
import { JOB_TONE, jobResult, nextRun, scheduleDay, siteName, stamp, took } from "../lib.js";

const STATUS = { ok: "OK", failed: "Failed", late: "Overdue", never: "Not run yet" };
const WEEK = [[1, "Mon"], [2, "Tue"], [3, "Wed"], [4, "Thu"], [5, "Fri"], [6, "Sat"], [0, "Sun"]];
const ITEM_TONE = { ok: "up", failed: "down", skipped: "unknown", pending: "accent" };

// A job's recent runs with their logs, fetched when opened (logs can be 64 KB each).
function Runs({ name }) {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    const ctrl = new AbortController();
    getJob(name, ctrl.signal).then((d) => setRuns(d.runs),
      (e) => e.name !== "AbortError" && setError(e.message));
    return () => ctrl.abort();
  }, [name]);
  if (error) return <p className="muted">Could not load runs: {error}</p>;
  if (!runs) return <p className="muted">Loading runs…</p>;
  return (
    <ol className="runs">
      {runs.map((r) => (
        <li key={r.id}>
          <details>
            <summary>
              <span className={`st-${r.result === "ok" ? "up" : "down"}`}>{r.result}</span>
              <span>{stamp(r.started)}</span>
              <span className="muted">took {took(r.started, r.finished)}</span>
              <span>{r.summary}</span>
            </summary>
            {r.log ? <pre className="log">{r.log}</pre> : <p className="muted">No log sent.</p>}
          </details>
        </li>
      ))}
    </ol>
  );
}

// The week at a glance: which run happens on which day, today marked.
function Week({ jobs, now }) {
  const today = new Date(now * 1000).getDay();
  const other = jobs.filter((j) => !scheduleDay(j.schedule));
  return (
    <div className="week-wrap">
      <ol className="week" aria-label="The weekly schedule">
        {WEEK.map(([day, label]) => {
          const here = jobs.filter((j) => scheduleDay(j.schedule)?.day === day);
          return (
            <li key={day} className={`wday ${day === today ? "wday-today" : ""} ${here.length ? "wday-busy" : ""}`}>
              <span className="wday-name">{label}{day === today && <span className="wday-now">today</span>}</span>
              {here.map((j) => (
                <span key={j.name} className={`wday-job tone-bg-${JOB_TONE[j.status]}`}>
                  <span className="wday-time">{scheduleDay(j.schedule).time}</span> {j.title || j.name}
                </span>
              ))}
            </li>
          );
        })}
      </ol>
      {other.length > 0 && (
        <p className="week-other">
          Also: {other.map((j) => `${j.title || j.name} (${j.schedule || "on demand"})`).join("; ")}.
        </p>
      )}
    </div>
  );
}

function JobCard({ job, now, sites, open, onToggle }) {
  const tone = JOB_TONE[job.status];
  const items = job.last?.items ?? [];
  const next = nextRun(job.schedule, now);
  return (
    <article className={`jcard jcard-${tone}`}>
      <header className="jcard-head">
        <span className={`dot dot-${tone}`} aria-hidden="true" />
        <h3>{job.title || job.name}</h3>
        <span className={`cell-status st-${tone}`}>{STATUS[job.status] ?? job.status}</span>
      </header>
      {job.description && <p className="jcard-what">{job.description}</p>}
      {job.steps?.length > 0 && (
        <ol className="jcard-steps" aria-label="Steps">
          {job.steps.map((s, i) => <li key={i}>{s}</li>)}
        </ol>
      )}
      <p className="jcard-result">{jobResult(job, now)}</p>
      {items.length > 0 && items.length <= 3 && items.every((i) => i.detail) ? (
        <ul className="jcard-lines" aria-label="What the last run covered">
          {items.map((i) => (
            <li key={i.name}>
              <span className={`dot dot-${ITEM_TONE[i.result] ?? "unknown"}`} aria-hidden="true" />
              <strong>{i.name}</strong> <span>{i.detail}</span>
            </li>
          ))}
        </ul>
      ) : items.length > 0 && (
        <ul className="jcard-items" aria-label="What the last run covered">
          {items.map((i) => (
            <li key={i.name} className={`jitem tone-bg-${ITEM_TONE[i.result] ?? "unknown"}`}
                title={i.detail ? `${i.result}: ${i.detail}` : i.result}>
              {i.name}
            </li>
          ))}
        </ul>
      )}
      <footer className="jcard-foot">
        <span>{next ? `Next: ${next}` : job.schedule}</span>
        {Object.keys(sites ?? {}).length > 1 && <span>{siteName(sites, job.site)}</span>}
        {job.severity === "critical" && <span className="crow-pages" title="A failed or overdue run sends a page">pages on failure</span>}
        <span className="mono-name">{job.name}</span>
        <button className="link-button" aria-expanded={open} onClick={onToggle}>
          {open ? "Hide" : "Show"} run history
        </button>
      </footer>
      {open && <Runs name={job.name} />}
    </article>
  );
}

export default function Jobs({ jobs, now, sites }) {
  const [open, setOpen] = useState(null);
  if (!jobs.length) return <p className="empty">No scheduled runs configured.</p>;
  return (
    <>
      <Week jobs={jobs} now={now} />
      <div className="jcards">
        {jobs.map((j) => (
          <JobCard key={j.name} job={j} now={now} sites={sites} open={open === j.name}
                   onToggle={() => setOpen(open === j.name ? null : j.name)} />
        ))}
      </div>
    </>
  );
}
