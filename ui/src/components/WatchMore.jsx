import { useState } from "react";
import { send } from "../api.js";
import { CheckForm } from "./ConfigPage.jsx";

// Everything worth watching on one page: every host's unwatched services with
// Off / Watch / Alert each, a form for anything else (a public site, a device),
// and an import of an existing argus.yml.
const CHOICES = [["off", "Off"], ["watch", "Watch"], ["alert", "Alert"]];

function Choice({ value, onChange, label }) {
  return (
    <span className="seg seg-small" role="radiogroup" aria-label={label}>
      {CHOICES.map(([k, text]) => (
        <button key={k} type="button" role="radio" aria-checked={value === k}
                className={value === k ? `on on-${k}` : undefined} onClick={() => onChange(k)}>{text}</button>
      ))}
    </span>
  );
}

function SuggestedRow({ g, value, onChange, onDismiss, busy }) {
  return (
    <li>
      <span className="row-main">{g.container} <span className="muted mono">:{g.port}</span>
        <span className="row-sub muted"><span className="kind">{g.check.kind.toUpperCase()}</span> <span className="mono">{g.check.target}</span></span></span>
      <span className="row-act">
        <Choice value={value} onChange={onChange} label={`${g.container} :${g.port}`} />
        {onDismiss && <button type="button" className="btn btn-small btn-quiet" disabled={busy} onClick={onDismiss}
                              aria-label={`${g.container} :${g.port} is not needed`}>Not needed</button>}
      </span>
    </li>
  );
}

// Marking a suggestion not needed keeps it in its host's ignored list (see
// start.dismiss_key on the server); bringing it back removes it.
async function mark(gs, ignored, csrf) {
  for (const g of gs) {
    await send("PUT", `/api/companions/${g.host_id}/ignored`, { container: g.key, ignored }, csrf);
  }
}

function Dismissed({ dismissed, session, onAdded }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  if (!dismissed.length || !session?.signed_in) return null;
  const back = async (gs) => {
    setBusy(true);
    setError(null);
    try {
      await mark(gs, false, session.csrf);
      onAdded(`${gs.length === 1 ? `${gs[0].container} :${gs[0].port} is` : `${gs.length} services are`} suggested again.`);
    } catch (e) { setError(e.message); }
    setBusy(false);
  };
  return (
    <details className="plumbing">
      <summary className="small muted">Marked not needed ({dismissed.length})</summary>
      <ul className="rows">{dismissed.map((g) => (
        <li key={`${g.host}/${g.key}`}>
          <span className="row-main">{g.container} <span className="muted mono">:{g.port}</span>
            <span className="row-sub muted">{g.host}</span></span>
          <span className="row-act"><button type="button" className="btn btn-small" disabled={busy} onClick={() => back([g])}>Suggest again</button></span>
        </li>
      ))}</ul>
      {error && <p className="form-error" role="alert">{error}</p>}
    </details>
  );
}

function Suggested({ suggestions, dismissed, session, fleet, onAdded, onTab }) {
  const [pick, setPick] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const key = (g) => `${g.host}/${g.check.name}`;
  const chosen = suggestions.filter((g) => (pick[key(g)] ?? "off") !== "off");
  const alerts = chosen.filter((g) => pick[key(g)] === "alert").length;
  const hosts = [...new Set(suggestions.map((g) => g.host))];
  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      const config = await send("GET", "/api/config");
      const checks = chosen.map((g) => ({ ...g.check, severity: pick[key(g)] === "alert" ? "critical" : "warning", interval: 60 }));
      await send("POST", "/api/config/checks", { checks, base: config.base }, session.csrf);
      setPick({});
      onAdded(`${checks.length} check${checks.length === 1 ? "" : "s"} added${alerts ? `, ${alerts} of them alert you` : ""}.`);
    } catch (e) {
      setError(e.message);
    }
    setBusy(false);
  };
  const dismiss = async (gs) => {
    setBusy(true);
    setError(null);
    try {
      await mark(gs, true, session.csrf);
      setPick({});
      onAdded(`${gs.length === 1 ? `${gs[0].container} :${gs[0].port}` : `${gs.length} services`} marked not needed. Bring ${gs.length === 1 ? "it" : "them"} back under Watch more, Marked not needed.`);
    } catch (e) { setError(e.message); }
    setBusy(false);
  };
  const can = session?.signed_in;
  const row = (g) => (
    <SuggestedRow key={key(g)} g={g} value={pick[key(g)] ?? "off"} onChange={(v) => setPick({ ...pick, [key(g)]: v })}
                  onDismiss={can ? () => dismiss([g]) : null} busy={busy} />
  );
  if (!suggestions.length) {
    return fleet.length ? (
      <>
        <p className="muted small">Every port your containers publish is watched{dismissed.length ? " or marked not needed" : ""}. Add a website or a device by hand, or import an argus.yml.</p>
        <Dismissed dismissed={dismissed} session={session} onAdded={onAdded} />
      </>
    ) : (
      <p className="muted small">Once a host reports in, the services it runs show up here to watch with one click.
        Until then: <a href="#/hosts">add a host</a>, or <a href="#watch-more" onClick={(e) => { e.preventDefault(); onTab("one"); }}>add a website or service</a> by hand.</p>
    );
  }
  return (
    <>
      <p className="muted small">
        Every port your hosts' containers publish that no check covers yet. <strong>Watch</strong> shows a problem here;
        <strong> Alert</strong> also sends you an alert. A good rule: alert for what you or others use.
        Mark the rest <strong>Not needed</strong> and they stop showing here.
      </p>
      {hosts.map((h) => {
        const apps = suggestions.filter((g) => g.host === h && !g.internal);
        const plumbing = suggestions.filter((g) => g.host === h && g.internal);
        return (
          <div key={h} className="wm-host">
            <h3 className="eyebrow">{h}</h3>
            {apps.length > 0 && <ul className="rows">{apps.map(row)}</ul>}
            {plumbing.length > 0 && (
              <details className="plumbing">
                <summary className="small muted">Usually not worth watching ({plumbing.length})</summary>
                <ul className="rows">{plumbing.map(row)}</ul>
              </details>
            )}
          </div>
        );
      })}
      {session?.signed_in && (
        <div className="wm-foot">
          <button className="btn btn-primary" disabled={busy || chosen.length === 0} onClick={add}>
            {chosen.length ? `Add ${chosen.length} check${chosen.length === 1 ? "" : "s"}` : "Choose Watch or Alert above"}</button>
          {alerts > 0 && <span className="muted small">{alerts} will alert you</span>}
          {chosen.length === 0 && (
            <button className="btn" disabled={busy} onClick={() => dismiss(suggestions)}>
              {suggestions.length === 1 ? "Not needed" : `None of these: mark all ${suggestions.length} not needed`}</button>
          )}
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
      <Dismissed dismissed={dismissed} session={session} onAdded={onAdded} />
    </>
  );
}

const STATE = { new: "new", exists: "already here", covered: "already watched", invalid: "cannot use" };

function Import({ session, onAdded }) {
  const [text, setText] = useState("");
  const [items, setItems] = useState(null);
  const [pick, setPick] = useState(new Set());
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const read = async () => {
    setError(null);
    try {
      const out = await send("POST", "/api/config/import", { text }, session.csrf);
      setItems(out.checks);
      setPick(new Set(out.checks.filter((c) => c.state === "new").map((c) => c.name)));
    } catch (e) { setError(e.message); }
  };
  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      const config = await send("GET", "/api/config");
      const out = await send("POST", "/api/config/import", { text, names: [...pick], base: config.base }, session.csrf);
      setItems(null);
      setText("");
      onAdded(`${out.added} check${out.added === 1 ? "" : "s"} imported.`);
    } catch (e) { setError(e.message); }
    setBusy(false);
  };
  return (
    <>
      <p className="muted small">Paste an argus.yml, from another Argus or an older one. Argus lists its checks, marks the ones you have, and adds the ones you tick.</p>
      {!items ? (
        <>
          <textarea className="yaml" value={text} onChange={(e) => setText(e.target.value)} placeholder={"checks:\n  - {name: shop, kind: http, target: https://shop.example/}"} spellCheck="false" />
          <div className="actions"><button className="btn btn-primary" disabled={!text.trim()} onClick={read}>Read it</button></div>
        </>
      ) : (
        <>
          <ul className="rows">{items.map((c) => (
            <li key={c.name} className={c.state === "new" ? undefined : "muted"}>
              <input type="checkbox" disabled={c.state !== "new"} checked={pick.has(c.name)} aria-label={c.name}
                     onChange={(e) => { const n = new Set(pick); e.target.checked ? n.add(c.name) : n.delete(c.name); setPick(n); }} />
              <span className="row-main">{c.name}{c.severity === "critical" && <span className="chip chip-critical">alerts</span>}
                <span className="row-sub muted">{c.kind ? <><span className="kind">{c.kind.toUpperCase()}</span> <span className="mono">{c.target}</span></> : c.why}</span></span>
              <span className="row-note small">{c.covered_by ? `watched by ${c.covered_by}` : STATE[c.state]}</span>
              <span className="row-act" />
            </li>
          ))}</ul>
          <div className="wm-foot">
            <button className="btn btn-primary" disabled={busy || pick.size === 0} onClick={add}>Add {pick.size} check{pick.size === 1 ? "" : "s"}</button>
            <button className="btn" onClick={() => setItems(null)}>Back</button>
          </div>
        </>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </>
  );
}

export default function WatchMore({ suggestions, dismissed = [], session, sites, groups, hosts, tab, onTab, onAdded }) {
  const tabs = [["suggested", `Suggested${suggestions.length ? ` (${suggestions.length})` : ""}`], ["one", "A website or service"], ["import", "Import"]];
  return (
    <section className="panel watch-more" id="watch-more">
      <div className="wm-head">
        <h2 className="eyebrow">Watch more</h2>
        <nav className="subtabs wm-tabs">{tabs.map(([k, label]) => (
          <a key={k} href="#watch-more" aria-current={tab === k ? "page" : undefined}
             onClick={(e) => { e.preventDefault(); onTab(k); }}>{label}</a>
        ))}</nav>
      </div>
      {tab === "suggested" && <Suggested suggestions={suggestions} dismissed={dismissed} session={session} fleet={hosts ?? []} onAdded={onAdded} onTab={onTab} />}
      {tab === "one" && (
        <CheckForm session={session} check={null} sites={sites} groups={groups} hosts={hosts}
                   onDone={() => onAdded("Check added.")} onCancel={() => onTab("suggested")} />
      )}
      {tab === "import" && <Import session={session} onAdded={onAdded} />}
    </section>
  );
}
