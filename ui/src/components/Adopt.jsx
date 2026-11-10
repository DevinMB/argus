import { useEffect, useState } from "react";
import { send } from "../api.js";

// "Deploy from git" for a stack that already runs on a host: a preview of what
// goes where, anything blocking it with its fix, then one button.
export default function Adopt({ host, project, session, onDone, onCancel }) {
  const [pv, setPv] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const base = `/api/adopt/${encodeURIComponent(host)}/${encodeURIComponent(project.project)}`;

  const read = () => send("POST", `${base}/read`, {}, session.csrf).then(setPv, (e) => setError(e.message));
  useEffect(() => { read(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (pv?.state !== "collecting") return undefined;
    const t = setTimeout(() => send("GET", base).then(setPv, (e) => setError(e.message)), 2500);
    return () => clearTimeout(t);
  }, [pv, base]);

  const adopt = async () => {
    setBusy(true);
    setError(null);
    try {
      await send("POST", base, {}, session.csrf);
      setDone(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="adopt">
      <h5 className="eyebrow">{project.link ? `link ${project.project}` : `deploy ${project.project} from git`}</h5>
      {done ? (
        <>
          <p className="form-ok">{pv.mode === "link" ? "Linked" : "Adopted"}. It keeps running in <code>{pv.dir}</code>;
            from now on a change merged to <code>{pv.path}</code> deploys it here.</p>
          <div className="actions"><button className="btn" onClick={onDone}>Done</button></div>
        </>
      ) : (
        <>
          {(!pv || pv.state === "collecting") && !error && <p className="muted small">Reading its files on {host}…</p>}
          {pv?.state === "error" && <p className="form-error">Could not read it: {pv.error}</p>}
          {pv?.state === "ready" && (
            <>
              <p className="small">It stays in <code>{pv.dir}</code> on {host}. Nothing moves, and data next to
                it (like <code>./data</code>) is never touched.</p>
              {pv.mode === "link" ? (
                <p className="small">
                  It is already in the deploy repo, at <code>{pv.path}/</code>: Argus deploys git's version of it,
                  in this folder. Nothing is written to git.
                  {pv.differs.length > 0 && (
                    <span className="st-failing"> The running {pv.differs.join(", ")} differs from git's; linking
                      deploys git's version.</span>
                  )}
                </p>
              ) : (
                <>
                  <p className="small">To the deploy repo, in <code>{pv.path}/</code>:</p>
                  {Object.entries(pv.files).map(([name, text]) => (
                    <details key={name}><summary className="mono small">{name}</summary><pre className="log small">{text}</pre></details>
                  ))}
                </>
              )}
              <p className="small">
                {pv.secrets.length
                  ? <>From its <code>.env</code>, kept encrypted in Argus and never in git: <span className="mono">{pv.secrets.join(", ")}</span>.</>
                  : "No .env secrets."}
                {pv.unreadable_env_lines > 0 && <span className="st-failing"> {pv.unreadable_env_lines} line(s) of its .env were not KEY=value and are left out.</span>}
              </p>
              {pv.blockers.length > 0 && (
                <ul className="blockers">
                  {pv.blockers.map((b) => (
                    <li key={b.text} className="st-failing small">
                      {b.text} {b.href && <a href={b.href}>Go there</a>}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="actions">
            {pv?.state === "ready" && (
              <button className="btn btn-primary" disabled={busy || pv.blockers.length > 0} onClick={adopt}>
                {busy ? "Working…" : pv.mode === "link" ? "Link" : "Adopt"}
              </button>
            )}
            {(pv?.state === "ready" || pv?.state === "error") && <button className="btn" onClick={read}>Read again</button>}
            <button className="btn" onClick={onCancel}>Cancel</button>
          </div>
        </>
      )}
    </div>
  );
}
