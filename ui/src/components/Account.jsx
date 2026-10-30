import { useState } from "react";
import { send } from "../api.js";

// The admin account and who may view the dashboard.
export default function Account({ session, onSession }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);

  const run = async (fn, ok) => {
    setError(null);
    setMsg(null);
    try {
      onSession(await fn());
      if (ok) setMsg(ok);
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    }
  };

  const change = (e) => {
    e.preventDefault();
    run(() => send("POST", "/api/password", { current, new: next }, session.csrf),
        "Password changed. Other browsers are signed out.").then((done) => {
      if (done) { setCurrent(""); setNext(""); }
    });
  };

  return (
    <section className="settings">
      <h1>Account</h1>

      <div className="panel">
        <h2 className="eyebrow">Who can see the dashboard</h2>
        <label className="toggle">
          <input type="checkbox" checked={session.view_requires_login}
                 onChange={(e) => run(() => send("PUT", "/api/access",
                   { view_requires_login: e.target.checked }, session.csrf))} />
          <span>
            <strong>Require sign-in to view</strong>
            <small>
              Off: anyone who can reach Argus sees the checks; only settings need the password.
              On: everything does, apart from /metrics for Prometheus and the timers' reports.
            </small>
          </span>
        </label>
      </div>

      <div className="panel">
        <h2 className="eyebrow">Settings</h2>
        <p><a href="#/config">Config</a>: where argus.yml lives, the whole file, export. · <a href="#/github">GitHub</a>: repos, deploys, tokens.</p>
      </div>

      <form className="panel" onSubmit={change}>
        <h2 className="eyebrow">Change password</h2>
        <input type="text" name="username" value="admin" autoComplete="username" readOnly hidden />
        <label className="field">
          <span>Current password</span>
          <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)}
                 autoComplete="current-password" required />
        </label>
        <label className="field">
          <span>New password</span>
          <input type="password" value={next} onChange={(e) => setNext(e.target.value)}
                 autoComplete="new-password" minLength={10} required />
          <small>At least 10 characters. Changing it signs out every other browser.</small>
        </label>
        <div className="actions">
          <button className="btn">Change password</button>
        </div>
      </form>

      {msg && <p className="form-ok" role="status">{msg}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}

      <div className="actions">
        <button className="btn" onClick={() => run(() => send("POST", "/api/logout", {}, session.csrf))}>
          Sign out
        </button>
      </div>
    </section>
  );
}
