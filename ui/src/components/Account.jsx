import { useState } from "react";
import { send } from "../api.js";

// Argus's own backup: an encrypted export, and restoring one.
function Backup({ session }) {
  const [password, setPassword] = useState("");
  const [phrase, setPhrase] = useState("");
  const [again, setAgain] = useState("");
  const [file, setFile] = useState(null);
  const [rphrase, setRphrase] = useState("");
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);

  const take = async (e) => {
    e.preventDefault();
    setError(null); setMsg(null);
    if (phrase !== again) return setError("The two passphrases differ.");
    try {
      const blob = await send("POST", "/api/export", { password, passphrase: phrase }, session.csrf);
      const url = URL.createObjectURL(new Blob([JSON.stringify(blob, null, 1)], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `argus-export-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      setPassword(""); setPhrase(""); setAgain("");
      setMsg("Exported. Keep the file somewhere other than this host, and the passphrase somewhere other than the file.");
    } catch (err) {
      setError(err.message);
    }
  };
  const restore = async (e) => {
    e.preventDefault();
    setError(null); setMsg(null);
    try {
      const data = await file.text();
      const out = await send("POST", "/api/import", { data, passphrase: rphrase }, session.csrf);
      setMsg(`Restored ${out.hosts} host(s) and ${out.secrets} secret(s).${out.restart ? " Restart Argus to use the restored certificate." : ""} Sign in with the restored install's password.`);
      setTimeout(() => window.location.reload(), 4000);
    } catch (err) {
      setError(err.message);
    }
  };
  return (
    <div className="panel">
      <h2 className="eyebrow">Back up Argus</h2>
      <p className="muted">
        One encrypted file with Argus's settings, secrets, hosts and certificate, so a new install
        carries on where this one stopped. Without it, losing this volume means setting up alerts,
        GitHub and every host again.
      </p>
      <form onSubmit={take} className="form-grid">
        <input type="text" name="username" value="admin" autoComplete="username" readOnly hidden />
        <label className="field"><span>Admin password</span>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required /></label>
        <label className="field"><span>A passphrase for the file</span>
          <input type="password" value={phrase} onChange={(e) => setPhrase(e.target.value)} autoComplete="new-password" minLength={12} required /></label>
        <label className="field"><span>The passphrase again</span>
          <input type="password" value={again} onChange={(e) => setAgain(e.target.value)} autoComplete="new-password" required /></label>
        <div className="actions wide"><button className="btn btn-primary">Export</button></div>
      </form>
      <details>
        <summary className="small">Restore from an export</summary>
        <form onSubmit={restore} className="form-grid">
          <label className="field"><span>Export file</span>
            <input type="file" accept="application/json,.json" onChange={(e) => setFile(e.target.files[0])} required /></label>
          <label className="field"><span>Its passphrase</span>
            <input type="password" value={rphrase} onChange={(e) => setRphrase(e.target.value)} autoComplete="off" required /></label>
          <p className="small st-failing wide">This replaces this install's settings, secrets and hosts.</p>
          <div className="actions wide"><button className="btn">Restore</button></div>
        </form>
      </details>
      {msg && <p className="form-ok" role="status">{msg}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

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

      <Backup session={session} />

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
