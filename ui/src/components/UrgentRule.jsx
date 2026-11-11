import { useEffect, useState } from "react";
import { getAlerts, send } from "../api.js";

const INSTALL_AT = [[9, "9.0 or more (critical)"], [8, "8.0 or more"], [7, "7.0 or more (high)"], [null, "Never, by score"]];
const pick = (v) => (v === "" ? null : Number(v));

// Which security fixes do not wait for the weekly window. Shown on Hosts,
// Patching; it loads and saves the same setting as the alert line above.
export default function UrgentRule({ session }) {
  const [sec, setSec] = useState(null);
  const [note, setNote] = useState(null);
  useEffect(() => { getAlerts().then((v) => setSec(v.security), () => {}); }, []);
  if (!sec) return null;
  const save = async (change) => {
    try {
      const v = await send("PUT", "/api/alerts", { security: { ...sec, ...change } }, session.csrf);
      setSec(v.security);
      setNote({ ok: true, text: "Saved." });
    } catch (e) { setNote({ ok: false, text: e.message }); }
  };
  return (
    <div className="urgent-rule">
      <p className="muted small">Argus looks up each pending fix's CVEs: their CVSS score (out of 10) from the public CVE records,
        and whether CISA lists them as being exploited in real attacks. These go in the same night, at the host's usual hour,
        under the same safety net, on every host with <strong>Install urgent security fixes the same night</strong> on.</p>
      <label className="toggle">
        <input type="checkbox" checked={sec.install_exploited} onChange={(e) => save({ install_exploited: e.target.checked })} />
        <span>A fix for a flaw being exploited in real attacks</span>
      </label>
      <label className="field">
        <span>A fix whose CVE scores</span>
        <select value={sec.install_min ?? ""} onChange={(e) => save({ install_min: pick(e.target.value) })}>
          {INSTALL_AT.map(([v, l]) => <option key={l} value={v ?? ""}>{l}</option>)}
        </select>
        <small>A lower line means more nights with a run: a kernel collects 7s most weeks.
          When to be told about a fix that waits is set on <a href="#/alerts?tab=what">Alerts, What alerts you</a>.</small>
      </label>
      {note && <p className={note.ok ? "form-ok" : "form-error"} role={note.ok ? "status" : "alert"}>{note.text}</p>}
    </div>
  );
}
