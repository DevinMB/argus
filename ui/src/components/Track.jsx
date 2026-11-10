import { agoText, safeHref } from "../lib.js";

const WORD = {
  done: "done", running: "running", waiting: "waits for you", failed: "failed",
  pending: "pending", not_wired: "not wired", paused: "paused",
};

// One change's path from update PR to healthy (argus github.track), each step
// with its detail.
export default function Track({ steps, now }) {
  return (
    <ol className="track">
      {steps.map((s) => {
        const href = safeHref(s.url);
        return (
          <li key={s.key} className={`step step-${s.status}`}>
            <span className="step-node" aria-hidden="true" />
            <div className="step-body">
              <div className="step-head">
                <span className="step-title">{s.title}</span>
                <span className="step-status">{WORD[s.status] ?? s.status}</span>
                {s.at && <time>{agoText(now - s.at)}</time>}
              </div>
              <div className="step-detail">
                {href ? <a href={href} target="_blank" rel="noreferrer">{s.detail}</a> : s.detail}
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
