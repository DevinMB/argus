import { useEffect, useRef, useState } from "react";
import { getCheck } from "../api.js";
import Track from "./Track.jsx";
import { ago, agoText, ms, siteName, stamp } from "../lib.js";

// Detail for one check: its settings, last result, and a timeline of every
// change of state Argus recorded, newest first.
export default function CheckDrawer({ name, now, sites, onClose, onEdit, onRemove }) {
  const [confirm, setConfirm] = useState(false);
  const [check, setCheck] = useState(null);
  const [error, setError] = useState(null);
  const closeRef = useRef(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setCheck(null);
    setError(null);
    getCheck(name, ctrl.signal).then(setCheck).catch((e) => {
      if (e.name !== "AbortError") setError(e.message);
    });
    return () => ctrl.abort();
  }, [name]);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="scrim" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={name}
             onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <div>
            <div className="eyebrow">check</div>
            <h2>{name}</h2>
          </div>
          <button ref={closeRef} className="close" onClick={onClose} aria-label="Close">×</button>
        </header>
        {error && <p className="banner banner-error">{error}</p>}
        {!check && !error && <p className="muted">Loading…</p>}
        {check && (
          <>
            <div className={`drawer-status st-${check.status}`}>
              <span className={`dot dot-${check.status}`} />
              <span className="status-word">{check.status}</span>
              <span className="muted">for {ago(now - check.last_change)}</span>
            </div>
            {check.last_error && <pre className="err">{check.last_error}</pre>}
            {onEdit && (
              <div className="actions">
                <button className="btn" onClick={() => onEdit(check)}>Change</button>
                {!confirm && <button className="btn" onClick={() => setConfirm(true)}>Remove…</button>}
                {confirm && (
                  <>
                    <span className="small">Stop checking {name}? Its history goes too.</span>
                    <button className="btn" onClick={() => onRemove(check)}>Remove</button>
                    <button className="link-button" onClick={() => setConfirm(false)}>Keep</button>
                  </>
                )}
              </div>
            )}
            <dl className="facts">
              <dt>Target</dt><dd className="mono">{check.target}</dd>
              <dt>Kind</dt><dd>{check.kind}{check.kind === "http" && ` · expects ${check.expect_status.join("/")}${check.verify_tls ? "" : " · TLS not verified"}`}</dd>
              <dt>Checked from</dt><dd>{check.companion ? <a href="#/hosts">{check.companion}</a> : "Argus"}</dd>
              <dt>Site</dt><dd>{siteName(sites, check.site)}</dd>
              <dt>Group</dt><dd>{check.group}</dd>
              <dt>Severity</dt><dd>{check.severity === "critical" ? "critical (pages)" : "warning (boards only)"}</dd>
              <dt>Every</dt><dd>{check.interval} s, timeout {check.timeout} s</dd>
              <dt>Last run</dt><dd>{agoText(now - check.last_run)} · {ms(check.last_duration)}</dd>
              <dt>Last success</dt><dd>{check.last_ok ? agoText(now - check.last_ok) : "never"}</dd>
            </dl>
            <h3 className="eyebrow">Track{check.repo && <span className="count">{check.repo}</span>}</h3>
            {check.track ? (
              <Track steps={check.track} now={now} />
            ) : (
              <p className="muted">No pipeline feeds this check yet; it is watched, not patched.</p>
            )}
            <h3 className="eyebrow">History</h3>
            {check.transitions.length === 0 ? (
              <p className="muted">No changes recorded.</p>
            ) : (
              <ol className="timeline">
                {check.transitions.map((t, i) => (
                  <li key={i} className={`event to-${t.to_status}`}>
                    <span className="node" aria-hidden="true" />
                    <div className="note">
                      <div className="note-head">
                        <span className={`st-${t.from_status}`}>{t.from_status}</span>
                        <span className="arrow">→</span>
                        <span className={`st-${t.to_status}`}>{t.to_status}</span>
                        <time>{stamp(t.ts)}</time>
                      </div>
                      {t.error && <div className="note-body mono">{t.error}</div>}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
