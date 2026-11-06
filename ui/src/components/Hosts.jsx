import { useEffect, useState } from "react";
import { send } from "../api.js";
import Adopt from "./Adopt.jsx";
import { agoText, backupLabel, bytes, containerTrouble, diskTone, hostSentence, sortContainers } from "../lib.js";

const STATUS = { ok: ["up", "reporting"], quiet: ["down", "gone quiet"], waiting: ["unknown", "waiting"] };
const FIRST_CONTAINERS = 8;

function Copy({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn" type="button" onClick={() => navigator.clipboard?.writeText(text).then(
      () => { setDone(true); setTimeout(() => setDone(false), 2000); }, () => {})}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}

// "Add a host": one command with a single-use code, then wait for the host to appear.
function AddHost({ session, hosts, tlsPin, onClose }) {
  const [url, setUrl] = useState(window.location.origin);
  const [name, setName] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [made, setMade] = useState(null);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);

  const make = async (e) => {
    e.preventDefault();
    setError(null);
    try {
      const out = await send("POST", "/api/companions/codes", { url, name, read_only: readOnly }, session.csrf);
      setMade({ ...out, before: new Set(hosts.map((h) => h.id)) });
    } catch (err) {
      setError(err.message);
    }
  };

  const joined = made && hosts.find((h) => !made.before.has(h.id));
  const left = made ? Math.max(0, Math.round(made.expires - now)) : 0;
  const plain = url.startsWith("http://");

  return (
    <form className="panel" onSubmit={make}>
      <h2 className="eyebrow">Add a host</h2>
      {!made && (
        <>
          <p className="muted">
            Run one command on the host. It installs the companion, a small program that reports
            the host's containers, disks and updates to Argus once a minute. It only ever calls
            Argus; nothing on the host listens for connections.
          </p>
          <label className="field">
            <span>Argus's address, as the host reaches it</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} required spellCheck="false" />
            {plain && (
              <small className="st-failing">
                Plain HTTP: the host's token would cross the network unencrypted. Use Argus's
                https:// address unless a proxy in front of Argus does HTTPS.
              </small>
            )}
            {!plain && !tlsPin && (
              <small>Argus is behind a proxy, so the host checks the proxy's certificate as usual.</small>
            )}
          </label>
          <label className="field">
            <span>Name (optional)</span>
            <input value={name} onChange={(e) => setName(e.target.value.toLowerCase())} placeholder="the host's own name"
                   pattern="[a-z0-9][a-z0-9\-]{0,62}" spellCheck="false" />
            <small>Lowercase letters, digits and dashes. Checks in argus.yml use it in <code>companion:</code>.</small>
          </label>
          <label className="toggle">
            <input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} />
            <span>
              <strong>Monitoring only</strong>
              <small>This host reports but will never deploy anything, whatever Argus says later.</small>
            </span>
          </label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="actions">
            <button className="btn btn-primary">Make the command</button>
            <button className="btn" type="button" onClick={onClose}>Cancel</button>
          </div>
        </>
      )}
      {made && !joined && (
        <>
          <p>Run this on the host as a user who can sudo:</p>
          <pre className="snippet command">{made.command}</pre>
          <p className="muted small">
            Logged in as root on a host without sudo, as in many Alpine or Proxmox containers? Leave
            out <code>sudo</code>. Alpine needs curl first: <code>apk add curl</code>.
          </p>
          <div className="actions">
            <Copy text={made.command} />
            <button className="btn" type="button" onClick={onClose}>Done</button>
          </div>
          <p className="muted" aria-live="polite">
            {left > 0
              ? <>Waiting for the host… The code works once, for {Math.floor(left / 60)}m {left % 60}s more.</>
              : <>The code has expired. <button className="link-button" type="button" onClick={() => setMade(null)}>Make a new one</button>.</>}
          </p>
        </>
      )}
      {joined && (
        <>
          <p className="form-ok" role="status"><strong>{joined.name}</strong> joined. {hostSentence(joined)}</p>
          <div className="actions"><button className="btn" type="button" onClick={onClose}>Done</button></div>
        </>
      )}
    </form>
  );
}

function Containers({ list }) {
  const [all, setAll] = useState(false);
  const sorted = sortContainers(list);
  const shown = all ? sorted : sorted.slice(0, FIRST_CONTAINERS);
  if (!list.length) return <p className="muted small">Docker runs, with no containers.</p>;
  return (
    <>
      <ul className="host-list">
        {shown.map((c) => {
          const tone = containerTrouble(c) ? "down" : c.health === "starting" ? "failing" : "up";
          return (
            <li key={c.name}>
              <span className={`dot dot-${tone}`} />
              <span className="host-item">{c.name}</span>
              <span className={`st-${tone} host-note`}>{c.health || c.state}</span>
              <span className="muted host-sub mono">{c.image}</span>
            </li>
          );
        })}
      </ul>
      {sorted.length > FIRST_CONTAINERS && (
        <button className="link-button small" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${sorted.length}`}
        </button>
      )}
    </>
  );
}

function Disks({ disks }) {
  return (
    <ul className="host-list">
      {disks.map((d) => {
        const share = d.total ? d.used / d.total : 0;
        return (
          <li key={d.mount} className="disk">
            <span className="host-item mono">{d.mount}</span>
            <span className={`st-${diskTone(d)} host-note`}>{Math.round(share * 100)}%</span>
            <span className="bar" aria-hidden="true">
              <span className={`fill-${diskTone(d)}`} style={{ width: `${Math.min(100, share * 100)}%` }} />
            </span>
            <span className="muted host-sub">{bytes(d.total - d.used)} free of {bytes(d.total)}</span>
          </li>
        );
      })}
    </ul>
  );
}

const STACK = {
  deployed: ["up", "deployed"], pending: ["failing", "deploying"], failed: ["down", "failed"],
  refused: ["down", "refused by policy"], rolled_back: ["down", "rolled back"],
  read_only: ["unknown", "not deployed: monitoring only"],
  link: ["accent", "waiting to be linked"],
};

// A stack's secrets: names are shown, values are write-only.
function Secrets({ stack, session, onSaved }) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState(null);
  const change = async (env) => {
    setError(null);
    try {
      await send("PUT", "/api/deploys/secrets", { path: stack.path, env }, session.csrf);
      setKey(""); setValue("");
      onSaved();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <div className="secrets">
      {stack.secrets.length > 0 && (
        <ul className="host-list">{stack.secrets.map((k) => (
          <li key={k}><span className="dot dot-accent" /><span className="host-item mono">{k}</span>
            <button className="link-button small" onClick={() => change({ [k]: null })}>remove</button></li>
        ))}</ul>
      )}
      <form className="field-row" onSubmit={(e) => { e.preventDefault(); change({ [key]: value }); }}>
        <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="NAME" aria-label="Secret name"
               pattern="[A-Za-z_][A-Za-z0-9_]*" required spellCheck="false" />
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)} placeholder="value"
               aria-label="Secret value" autoComplete="off" />
        <button className="btn">Set</button>
      </form>
      <small className="muted">Written to the stack's .env on the host (root only). Changing one redeploys the stack.</small>
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

function Stacks({ stacks, session, reload }) {
  const [open, setOpen] = useState(null);
  return (
    <ul className="host-list">
      {stacks.map((st) => {
        const [tone, word] = STACK[st.status] ?? ["unknown", st.status];
        return (
          <li key={st.path} className="stack">
            <span className={`dot dot-${tone}`} />
            <span className="host-item">{st.name}</span>
            <span className={`st-${tone} host-note`}>{word}</span>
            <span className="muted host-sub mono">{st.path}{st.at ? ` · ${agoText(Date.now() / 1000 - st.at)}` : ""}</span>
            {st.status === "link" && (
              <span className="host-wide small muted">Runs from <code>{st.held}</code>, outside Argus. Link it below
                to deploy it from git where it is.</span>
            )}
            {st.detail && st.status !== "deployed" && st.status !== "link" && <pre className="host-wide log small">{st.detail}</pre>}
            {session?.signed_in && (
              <button className="link-button small host-wide secrets-toggle" onClick={() => setOpen(open === st.path ? null : st.path)}>
                Secrets ({st.secrets.length})
              </button>
            )}
            {open === st.path && <div className="host-wide"><Secrets stack={st} session={session} onSaved={reload} /></div>}
          </li>
        );
      })}
    </ul>
  );
}

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const RUN_TONE = { ok: "up", failed: "down", running: "accent", granted: "accent", rebooting: "accent" };

function PatchForm({ host, session, refs, backups, onSaved, onCancel }) {
  const suggested = host.patching.suggested_backup;
  const [p, setP] = useState(() => {
    const plan = host.patching.plan;
    // A guest whose Proxmox backup Argus can see starts with it chosen.
    return !plan.backup && suggested ? { ...plan, backup: suggested } : plan;
  });
  const [error, setError] = useState(null);
  const set = (k, v) => setP({ ...p, [k]: v });
  const save = async (e) => {
    e.preventDefault();
    try {
      await send("PUT", `/api/patching/${host.name}`, { ...p, day: Number(p.day) }, session.csrf);
      onSaved();
    } catch (err) {
      setError(err.message);
    }
  };
  const who = p.elsewhere ? "elsewhere" : "argus";
  const pick = (w) => setP({ ...p, elsewhere: w === "elsewhere", enabled: w === "argus" });
  return (
    <form className="patch-form" onSubmit={save}>
      <label className="toggle"><input type="radio" checked={who === "argus"} onChange={() => pick("argus")} />
        <span><strong>Argus patches it every week</strong></span></label>
      <label className="toggle"><input type="radio" checked={who === "elsewhere"} onChange={() => pick("elsewhere")} />
        <span><strong>Something else patches it</strong>
          <small>Ansible, unattended-upgrades, your hypervisor's tools. Argus then never patches it, so two tools never fight over one host.</small></span></label>
      {who === "elsewhere" && (
        <label className="field"><span>The job or timer that patches it (optional)</span>
          <select value={p.elsewhere_job} onChange={(e) => set("elsewhere_job", e.target.value)}>
            <option value="">None reports to Argus</option>
            {refs.map((r) => <option key={r} value={r}>{r.replace(":", ": ")}</option>)}
          </select>
          <small>Name it and the host card shows how its last run went; a failed or late run already pages.</small>
        </label>
      )}
      {who === "argus" && <>
      <div className="form-grid">
        <label className="field"><span>Day</span>
          <select value={p.day} onChange={(e) => set("day", Number(e.target.value))}>
            {DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
          </select></label>
        <label className="field"><span>Time (the host's clock)</span>
          <input type="time" value={p.time} onChange={(e) => set("time", e.target.value)} required /></label>
        <label className="field"><span>Updates</span>
          <select value={p.level} onChange={(e) => set("level", e.target.value)}>
            <option value="full">All of them</option>
            <option value="security">Security only</option>
          </select></label>
        <label className="field"><span>Its backup</span>
          <select value={p.backup} onChange={(e) => set("backup", e.target.value)}>
            <option value="">None yet</option>
            {backups.length > 0 && (
              <optgroup label="Proxmox backups">
                {backups.map((b) => <option key={b.ref} value={b.ref}>{backupLabel(b)}{b.ref === suggested ? " (this host)" : ""}</option>)}
              </optgroup>
            )}
            {refs.length > 0 && (
              <optgroup label="Jobs and timers that report to Argus">
                {refs.map((r) => <option key={r} value={r}>{r.replace(":", ": ")}</option>)}
              </optgroup>
            )}
          </select></label>
        <label className="field"><span>Recent means (days)</span>
          <input type="number" min="1" max="366" value={p.backup_days}
            onChange={(e) => set("backup_days", Number(e.target.value))} required /></label>
        <label className="field"><span>Its restore test (optional)</span>
          <select value={p.restore} onChange={(e) => set("restore", e.target.value)}>
            <option value="">None</option>
            {refs.map((r) => <option key={r} value={r}>{r.replace(":", ": ")}</option>)}
          </select></label>
      </div>
      <small className="muted">
        Argus patches a host only when it can be brought back: its backup is more recent than the
        days you set here, or the host has a ZFS root Argus snapshots first. On Proxmox, add Argus to
        the Proxmox server too and each guest's vzdump backup shows up here by itself; otherwise
        pick a job or timer that reports to Argus when your backup finishes. A restore test now
        and then is how you know the backup works: optional, and encouraged.
      </small>
      <label className="toggle"><input type="checkbox" checked={p.reboot} onChange={(e) => set("reboot", e.target.checked)} />
        <span>Reboot when an update needs it</span></label>
      </>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="actions">
        <button className="btn btn-primary">Save</button>
        <button className="btn" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function Patching({ host, session, refs, backups, reload }) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState(null);
  const pt = host.patching;
  if (!pt) return null;
  const last = pt.runs[0];
  const now = async () => {
    try {
      await send("POST", `/api/patching/${host.name}/now`, {}, session.csrf);
      reload();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <section>
      <h4 className="eyebrow">patching</h4>
      {pt.plan.elsewhere ? (
        <p className="small">
          Patched by something else{pt.elsewhere ? <>: {pt.elsewhere.title}{pt.elsewhere.schedule && `, ${pt.elsewhere.schedule}`}.{" "}
            <span className={`st-${{ ok: "up", failed: "down", late: "failing" }[pt.elsewhere.status] ?? "unknown"}`}>
              {pt.elsewhere.at ? `Last run ${pt.elsewhere.status === "ok" ? "ok" : pt.elsewhere.status} ${agoText(Date.now() / 1000 - pt.elsewhere.at)}` : "No run reported yet"}
            </span>{pt.elsewhere.summary && <span className="muted">: {pt.elsewhere.summary}</span>}</> : "."}
        </p>
      ) : (
        <p className="small">
          {pt.plan.enabled
            ? <>{pt.allowed[0].toUpperCase() + pt.allowed.slice(1)}, {DAYS[pt.plan.day]}s at {pt.plan.time} (host time).</>
            : "Not patched by Argus."}{" "}
          <span className="muted">Safety net: {pt.safety.net}.</span>
          {pt.plan.restore && !pt.safety.tested && <span className="muted"> No restore test in the last 35 days.</span>}
          {pt.plan.enabled && !pt.plan.restore && <span className="muted"> Never restore-tested: worth a try.</span>}
        </p>
      )}
      {pt.skipped && <p className="small st-failing">Not patched: {pt.skipped}.</p>}
      {last && (
        <p className="small">
          <span className={`st-${RUN_TONE[last.result] ?? "unknown"}`}>
            {last.result === "granted" ? "starting" : last.result}
          </span>{" "}
          {agoText(Date.now() / 1000 - last.requested)}{last.summary && `: ${last.summary}`}
        </p>
      )}
      {last?.log && last.result === "failed" && <pre className="log small">{last.log.slice(-1500)}</pre>}
      {pt.requested && <p className="small muted">Patching on its next report, within a minute.</p>}
      {session?.signed_in && !host.read_only && !editing && (
        <div className="actions">
          <button className="link-button small" onClick={() => setEditing(true)}>
            {pt.plan.enabled || pt.plan.elsewhere ? "Change how it is patched" : "Choose a window"}</button>
          {!pt.plan.elsewhere && (
            <button className="link-button small" onClick={now} disabled={pt.requested || last?.result === "running"}>Patch now</button>
          )}
        </div>
      )}
      {editing && <PatchForm host={host} session={session} refs={refs} backups={backups}
                             onSaved={() => { setEditing(false); reload(); }} onCancel={() => setEditing(false)} />}
      {error && <p className="form-error" role="alert">{error}</p>}
    </section>
  );
}

// A Proxmox server's guests: which have a backup, and which have Argus on them.
function Guests({ px, hostNames }) {
  const now = Date.now() / 1000;
  const without = px.guests.filter((g) => !hostNames.has(g.name.toLowerCase()));
  return (
    <section>
      <h4 className="eyebrow">guests</h4>
      <p className="muted small section-why">
        Your apps run in these VMs and containers. Add Argus to each one (Add a host, run on the
        guest) to see its containers, watch it and patch it; the backup shown here is its safety
        net, linked for you.{without.length > 0 && ` ${without.length} of ${px.guests.length} without Argus yet.`}
      </p>
      <ul className="host-list">{px.guests.map((g) => {
        const age = g.backup && now - g.backup.at;
        const tone = !g.backup ? "unknown" : age < 8 * 86400 ? "up" : "failing";
        return (
          <li key={`${g.type}${g.vmid}`}>
            <span className={`dot dot-${tone}`} />
            <span className="host-item">{g.name || "(no name)"} <span className="muted mono">{g.type === "lxc" ? "CT" : "VM"} {g.vmid}</span></span>
            <span className={`host-note ${hostNames.has(g.name.toLowerCase()) ? "st-up" : "muted"}`}>
              {hostNames.has(g.name.toLowerCase()) ? "has Argus" : g.status}</span>
            <span className="muted host-sub">
              {g.backup ? `Backed up ${agoText(age)} to ${g.backup.storage} (${bytes(g.backup.size)})` : "No backup this server can see"}
            </span>
          </li>
        );
      })}</ul>
      {px.error && <p className="st-failing small">{px.error}</p>}
    </section>
  );
}

function NotFromGit({ host, session, reload }) {
  const [open, setOpen] = useState(null);
  if (!host.adoptable?.length) return null;
  return (
    <section>
      <h4 className="eyebrow">running here, not deployed by Argus</h4>
      <p className="muted small section-why">
        Compose projects started by hand or another tool. <strong>Deploy from git</strong> puts each
        one's compose file in your deploy repo (its secrets stay in Argus) and from then on a merge
        redeploys it, in the same folder, with no restart if nothing changed.
      </p>
      <ul className="host-list suggest">{host.adoptable.map((p) => (
        <li key={p.project}>
          <span className="dot dot-unknown" />
          <span className="host-item">{p.project} <span className="muted mono">{p.dir}</span></span>
          {session?.signed_in && open !== p.project && (
            <button className="btn" onClick={() => setOpen(p.project)}>{p.link ? "Link" : "Deploy from git"}</button>
          )}
          {open === p.project && (
            <div className="host-wide">
              <Adopt host={host.name} project={p} session={session}
                     onDone={() => { setOpen(null); reload(); }} onCancel={() => setOpen(null)} />
            </div>
          )}
        </li>
      ))}</ul>
    </section>
  );
}

function Host({ host, session, onRemoved, suggestions, onWatch, refs, backups, hostNames }) {
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState(null);
  const r = host.report;
  const [tone, word] = STATUS[host.status];
  const remove = async () => {
    try {
      await send("DELETE", `/api/companions/${host.id}`, undefined, session.csrf);
      onRemoved();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <article className={`host host-${host.status}`}>
      <header className="host-head">
        <span className={`dot dot-${tone}`} />
        <h3>{host.name}</h3>
        <span className={`st-${tone}`}>{word}</span>
        {host.read_only && <span className="chip">monitoring only</span>}
        <span className="muted host-seen">
          {host.last_seen ? `reported ${agoText(host.age)}` : `enrolled ${agoText(Date.now() / 1000 - host.enrolled)}`}
        </span>
      </header>
      <p className="host-sentence">{hostSentence(host)}</p>
      {r && <p className="muted small">{[r.os, r.kernel, r.arch, r.version && `companion ${r.version}`, host.address].filter(Boolean).join(" · ")}</p>}

      {host.stacks?.length > 0 && (
        <section><h4 className="eyebrow">stacks it runs</h4><Stacks stacks={host.stacks} session={session} reload={onRemoved} /></section>
      )}
      {r?.containers?.length > 0 && <section><h4 className="eyebrow">containers</h4><Containers list={r.containers} /></section>}
      {r?.proxmox ? <Guests px={r.proxmox} hostNames={hostNames} />
        : r && !r.containers?.length && /-pve$/.test(r.kernel || "") && (
        <p className="muted small">
          This is a Proxmox server: your apps run in its VMs and containers, not on it. Update Argus
          on this server (run Add a host again) to see its guests and their backups here.
        </p>
      )}
      <NotFromGit host={host} session={session} reload={onRemoved} />
      {suggestions?.length > 0 && (
        <section>
          <h4 className="eyebrow">not watched yet</h4>
          <p className="muted small section-why">
            Ports these containers publish that no check covers. <strong>Watch</strong> adds a check,
            so Argus tells you when one stops answering.
          </p>
          <ul className="host-list suggest">{suggestions.map((g) => (
            <li key={g.port}><span className="dot dot-unknown" />
              <span className="host-item">{g.container} <span className="muted mono">:{g.port}</span></span>
              {session?.signed_in && <button className="btn" onClick={() => onWatch(g.check)}>Watch</button>}</li>
          ))}</ul>
        </section>
      )}
      {r?.docker_error && <p className="st-failing small">Docker: {r.docker_error}</p>}
      {r?.disks?.length > 0 && <section><h4 className="eyebrow">disks</h4><Disks disks={r.disks} /></section>}
      {r?.failed_units?.length > 0 && (
        <section>
          <h4 className="eyebrow">failed systemd units</h4>
          <ul className="host-list">{r.failed_units.map((u) => (
            <li key={u}><span className="dot dot-down" /><span className="host-item mono">{u}</span></li>
          ))}</ul>
        </section>
      )}
      <Patching host={host} session={session} refs={refs} backups={backups} reload={onRemoved} />
      {(r?.updates || r?.reboot_required) && (
        <section>
          <h4 className="eyebrow">updates</h4>
          <p className="small">
            {r.updates?.error ? <span className="st-failing">{r.updates.error}</span>
              : r.updates?.pending != null && (r.updates.pending
                ? `${r.updates.pending} waiting${r.updates.security != null ? `, ${r.updates.security} of them security` : ""} (${r.updates.manager}).`
                : `Up to date (${r.updates.manager}).`)}
            {r.reboot_required && <span className="st-failing"> Needs a reboot.</span>}
          </p>
        </section>
      )}
      {host.checks.length > 0 && (
        <section>
          <h4 className="eyebrow">checks run from here</h4>
          <ul className="host-list">{host.checks.map((c) => (
            <li key={c.name}>
              <span className={`dot dot-${c.status === "unknown" ? "unknown" : c.status}`} />
              <a className="host-item" href={`#/check/${encodeURIComponent(c.name)}`}>{c.name}</a>
              <span className={`st-${c.status} host-note`}>{c.status}</span>
              <span className="muted host-sub mono">{c.kind} {c.target}</span>
            </li>
          ))}</ul>
        </section>
      )}

      {session?.signed_in && (
        <div className="host-foot">
          {!confirm && <button className="link-button small" onClick={() => setConfirm(true)}>Remove…</button>}
          {confirm && (
            <span className="small">
              Remove {host.name}? Its token stops working at once; the program stays on the host
              until you uninstall it.{" "}
              <button className="btn" onClick={remove}>Remove</button>{" "}
              <button className="link-button" onClick={() => setConfirm(false)}>Keep</button>
            </span>
          )}
          {error && <p className="form-error" role="alert">{error}</p>}
        </div>
      )}
    </article>
  );
}

export default function Hosts({ data, session, reload, start, refs = [] }) {
  const [adding, setAdding] = useState(false);
  const hosts = data?.companions ?? [];
  const quiet = hosts.filter((h) => h.status === "quiet").length;
  const backups = data?.backups ?? [];
  const hostNames = new Set(hosts.flatMap((h) => [h.name, (h.report?.hostname ?? "").split(".")[0].toLowerCase()]).filter(Boolean));
  return (
    <section className="hosts-page">
      <div className="hosts-head">
        <div>
          <h1>Hosts</h1>
          <p className="lede">
            {hosts.length === 0 ? "No hosts yet. Add one to see its containers, disks and updates here."
              : `${hosts.length} host${hosts.length === 1 ? "" : "s"} report${hosts.length === 1 ? "s" : ""} in every minute${quiet ? `; ${quiet} ha${quiet === 1 ? "s" : "ve"} gone quiet and page${quiet === 1 ? "s" : ""}` : ""}. A host that misses three reports, or a disk past 95%, pages you.`}
          </p>
        </div>
        {!adding && (session?.signed_in
          ? <button className="btn btn-primary" onClick={() => setAdding(true)}>Add a host</button>
          : <a className="btn" href="#/account">Sign in to add a host</a>)}
      </div>
      {adding && session?.signed_in && (
        <AddHost session={session} hosts={hosts} tlsPin={data?.tls_pin}
                 onClose={() => { setAdding(false); reload(); }} />
      )}
      {data?.unknown?.length > 0 && (
        <p className="banner banner-warn">
          argus.yml has checks for {data.unknown.join(", ")}, which {data.unknown.length === 1 ? "is" : "are"} not
          added here yet. Those checks wait until {data.unknown.length === 1 ? "it reports" : "they report"}.
        </p>
      )}
      <div className="hosts">
        {hosts.map((h) => (
          <Host key={h.id} host={h} session={session} onRemoved={reload} refs={refs}
                backups={backups.filter((b) => b.server !== h.name)} hostNames={hostNames}
                suggestions={(start?.suggestions ?? []).filter((g) => g.host === h.name)}
                onWatch={(c) => { window.location.hash = ""; window.dispatchEvent(new CustomEvent("argus-watch", { detail: c })); }} />
        ))}
      </div>
    </section>
  );
}
