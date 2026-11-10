import { useState } from "react";
import { send } from "../api.js";

// First visit: set the admin password with the setup code from Argus's log.
// After that: the password.
export default function SignIn({ session, onSession, why }) {
  const setup = session?.setup_needed;
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (setup && password !== again) return setError("The two passwords differ.");
    setBusy(true);
    setError(null);
    try {
      onSession(setup
        ? await send("POST", "/api/setup", { code, password })
        : await send("POST", "/api/login", { password }));
      // A new install goes straight to its checklist on the home page.
      if (setup) window.location.hash = "";
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="signin">
      <form className="panel" onSubmit={submit}>
        <h1>{setup ? "Set up Argus" : "Sign in"}</h1>
        {setup ? (
          <p className="muted">
            Choose the admin password. To prove this is your Argus, enter the setup code from its
            log: <code>docker logs argus</code> shows a line with <code>Setup code:</code>.
          </p>
        ) : (
          <p className="muted">{why ?? "Settings need the admin password."}</p>
        )}
        {setup && (
          <label className="field">
            <span>Setup code</span>
            <input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off"
                   placeholder="abcd-1234-ef56" required spellCheck="false" />
          </label>
        )}
        {/* A username field, hidden, so password managers file this under Argus's admin. */}
        <input type="text" name="username" value="admin" autoComplete="username" readOnly hidden />
        <label className="field">
          <span>{setup ? "New password" : "Password"}</span>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                 autoComplete={setup ? "new-password" : "current-password"} required
                 minLength={setup ? 10 : undefined} autoFocus={!setup} />
          {setup && <small>At least 10 characters. A few random words work well.</small>}
        </label>
        {setup && (
          <label className="field">
            <span>Same password again</span>
            <input type="password" value={again} onChange={(e) => setAgain(e.target.value)}
                   autoComplete="new-password" required />
          </label>
        )}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="actions">
          <button className="btn btn-primary" disabled={busy}>{setup ? "Set password" : "Sign in"}</button>
        </div>
        {!setup && (
          <p className="muted small">
            Forgot it? Run <code>docker exec argus python -m argus reset-login</code>, then set a
            new one with the code from the log.
          </p>
        )}
      </form>
    </section>
  );
}
