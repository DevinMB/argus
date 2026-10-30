import { ago, siteName } from "../lib.js";

const TONE = { ok: "up", late: "down", never: "failing" };

// Timers on time are one line each; a late or silent one gets a full tile.
export default function Heartbeats({ heartbeats, now, sites }) {
  if (!heartbeats.length) return <p className="empty">No timers report here yet.</p>;
  const bad = heartbeats.filter((h) => h.status !== "ok");
  const good = heartbeats.filter((h) => h.status === "ok");
  return (
    <>
      {bad.length > 0 && (
        <div className="grid">
          {bad.map((h) => (
            <div key={h.name} className={`cell cell-${TONE[h.status]}`}>
              <div className="cell-top">
                <span className={`dot dot-${TONE[h.status]}`} aria-hidden="true" />
                <span className="cell-name">{h.name}</span>
                <span className={`cell-status st-${TONE[h.status]}`}>{h.status}</span>
              </div>
              {h.description && <div className="cell-target">{h.description}</div>}
              <div className="cell-foot">
                <span>{h.last_seen ? `beat ${ago(now - h.last_seen)} ago` : "never beat"}</span>
                <span>limit {ago(h.max_age)}</span>
                <span>{siteName(sites, h.site)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
      {good.length > 0 && (
        <div className="crows">
          {good.map((h) => (
            <div key={h.name} className="crow" title={h.description}>
              <span className="dot dot-up" aria-hidden="true" />
              <span className="crow-name">{h.name}</span>
              <span className="crow-target">beat {ago(now - h.last_seen)} ago</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
