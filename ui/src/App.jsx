import { useCallback, useEffect, useMemo, useState } from "react";
import { useArgus } from "./api.js";
import CheckDrawer from "./components/CheckDrawer.jsx";
import CheckGrid from "./components/CheckGrid.jsx";
import Asides from "./components/Asides.jsx";
import Heartbeats from "./components/Heartbeats.jsx";
import Jobs from "./components/Jobs.jsx";
import Meter from "./components/Meter.jsx";
import Ring from "./components/Ring.jsx";
import Updates from "./components/Updates.jsx";
import { acrossSites, ago, agoText, byStatus, filterChecks, groupChecks, headline, joinList, lifecycle, siteName, siteNote, summarize } from "./lib.js";

const STATUS_FILTERS = [["all", "All"], ["problems", "Problems"], ["up", "Up"]];

// The view and the selected check live in the hash (#/updates, #/updates/<repo>,
// #/check/<name>) so a link can open any of them. A check's drawer opens over
// the Checks view.
function useView() {
  const read = () => {
    const m = window.location.hash.match(/^#\/updates(?:\/(.+))?$/);
    return m ? ["updates", m[1] && decodeURIComponent(m[1])] : ["checks", null];
  };
  const [view, setView] = useState(read);
  useEffect(() => {
    const on = () => setView(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return view;
}

function useSelected() {
  const read = () => {
    const m = window.location.hash.match(/^#\/check\/(.+)$/);
    return m ? decodeURIComponent(m[1]) : null;
  };
  const [name, setName] = useState(read);
  useEffect(() => {
    const on = () => setName(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const select = useCallback((n) => {
    window.location.hash = n ? `/check/${encodeURIComponent(n)}` : "";
  }, []);
  return [name, select];
}

function Segmented({ value, options, onChange, label }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map(([v, text]) => (
        <button key={v} aria-pressed={value === v} onClick={() => onChange(v)}>{text}</button>
      ))}
    </div>
  );
}

// Footer links come from argus.yml, grouped and in config order.
function linkGroups(links = []) {
  const out = {};
  for (const l of links) (out[l.group] ??= []).push(l);
  return out;
}

export default function App() {
  const { data, error, fetchedAt } = useArgus();
  const [selected, select] = useSelected();
  const [view, focus] = useView();
  const [status, setStatus] = useState("all");
  const [site, setSite] = useState("all");
  const [q, setQ] = useState("");
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const checks = data?.checks ?? [];
  const sites = data?.info.sites ?? {};
  const siteOrder = Object.keys(sites).length ? Object.keys(sites) : [...new Set(checks.map((c) => c.site))];
  const manySites = siteOrder.length > 1;
  const siteFilters = [["all", "All sites"], ...siteOrder.map((s) => [s, siteName(sites, s)])];
  // Server time, advanced locally between refreshes, so ages use Argus's clock.
  const now = data ? data.info.now + (Date.now() - fetchedAt) / 1000 : Date.now() / 1000;
  const sum = useMemo(() => summarize(checks), [checks]);
  const problems = useMemo(
    () => checks.filter((c) => c.status === "down" || c.status === "failing").sort(byStatus),
    [checks],
  );
  const shown = useMemo(() => filterChecks(checks, { status, site, q, sites }), [checks, status, site, q, sites]);
  const siteSum = (s) => summarize(checks.filter((c) => c.site === s));
  const critical = checks.filter((c) => c.severity === "critical");
  const stalled = data && now - data.info.last_tick > 30;
  const needYou = (data?.updates?.repos ?? []).filter((r) => lifecycle(r).you).length;

  return (
    <div className="page">
      <header className="top">
        <a className="brand" href="#">
          <img src="/favicon.svg" alt="" width="22" height="22" />
          <span>ARGUS</span>
        </a>
        <nav className="tabs" aria-label="View">
          <a href="#" aria-current={view === "checks" ? "page" : undefined}>Checks</a>
          <a href="#/updates" aria-current={view === "updates" ? "page" : undefined}>
            Updates{needYou > 0 && <span className="tab-badge">{needYou}</span>}
          </a>
        </nav>
        <div className="top-meta">
          <span className={`live ${error || stalled ? "live-off" : ""}`}>
            {error ? "API unreachable" : stalled ? "scheduler stalled" : "live"}
          </span>
          {fetchedAt && <span className="hide-sm">updated {agoText((Date.now() - fetchedAt) / 1000)}</span>}
          {data && <span className="hide-sm">config {data.info.config}</span>}
          {data && <span className="hide-sm">build {data.info.commit.slice(0, 7)}</span>}
        </div>
      </header>

      {error && data && <p className="banner banner-error">Showing the last good data: {error}</p>}
      {data?.info.config_reload_ok === false && (
        <p className="banner banner-warn">
          The newest argus.yml was rejected; the previous config is still running.
        </p>
      )}

      {!data && !error && <p className="muted pad">Loading…</p>}
      {!data && error && <p className="banner banner-error">Cannot reach the Argus API: {error}</p>}

      {data && view === "updates" && <Updates updates={data.updates} now={now} focus={focus} />}

      {data && view === "checks" && (
        <>
          <section className="hero">
            <div className="eye">
              <Ring checks={checks} siteOrder={siteOrder} onPick={select} />
              <div className="eye-center">
                {sum.down ? (
                  <><div className="big st-down">{sum.down}</div><div className="eyebrow">down</div></>
                ) : (
                  <><div className="big">{sum.up}<span className="of">/{sum.total}</span></div>
                    <div className="eyebrow">checks up</div></>
                )}
              </div>
            </div>
            <div className="hero-side">
              <h1>{headline(sum)}</h1>
              <p className="lede">
                {checks.length} checks every minute{acrossSites(siteOrder.length)}, covering{" "}
                {joinList(groupChecks(checks).map(([g]) => data.info.groups?.[g] ?? g))}. Three
                failures in a row and a check is down; the critical ones page you.
              </p>
              <Asides checks={checks} heartbeats={data.heartbeats} />
              <div className="meters">
                {manySites && siteOrder.map((s) => {
                  const ss = siteSum(s);
                  return <Meter key={s} label={siteName(sites, s)} value={ss.up} total={ss.total}
                                tone={ss.down ? "down" : ss.failing ? "failing" : "up"}
                                sub={siteNote(ss)} />;
                })}
                <Meter label="Pages if down" value={critical.filter((c) => c.status === "up").length}
                       total={critical.length} tone="accent" sub="critical checks up" />
              </div>
            </div>
          </section>

          <section className="attention" aria-live="polite">
            {problems.length === 0 ? (
              <p className="quiet">Nothing needs attention.</p>
            ) : (
              <ul>
                {problems.map((c) => (
                  <li key={c.name}>
                    <button className="attn" onClick={() => select(c.name)}>
                      <span className={`dot dot-${c.status}`} />
                      <strong>{c.name}</strong>
                      <span className={`st-${c.status}`}>{c.status}</span>
                      <span className="muted">{c.last_error}</span>
                      <span className="muted">{ago(now - c.last_change)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="group">
            <h2 className="eyebrow">scheduled runs <span className="count">{data.jobs.length}</span></h2>
            <p className="group-hint">
              {data.info.jobs_intro || "Runs that start on their own and report back here when they finish. A failed or overdue run pages."}
            </p>
            <Jobs jobs={data.jobs} now={now} sites={sites} />
          </section>

          <section className="toolbar">
            <Segmented label="Status" value={status} options={STATUS_FILTERS} onChange={setStatus} />
            {manySites && <Segmented label="Site" value={site} options={siteFilters} onChange={setSite} />}
            <input className="search" type="search" placeholder="Filter by name, target or tag"
                   value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter checks" />
          </section>

          <CheckGrid checks={shown} now={now} sites={sites} groups={data.info.groups} onPick={select} />

          <section className="group">
            <h2 className="eyebrow">timers <span className="count">{data.heartbeats.length}</span></h2>
            <p className="group-hint">Background jobs that report in after each run; one that goes quiet is late.</p>
            <Heartbeats heartbeats={data.heartbeats} now={now} sites={sites} />
          </section>
        </>
      )}

      <footer className="foot">
        <div>
          <div className="eyebrow">Argus</div>
          <a href="/api/checks">/api/checks</a>
          <a href="/metrics">/metrics</a>
        </div>
        {Object.entries(linkGroups(data?.info.links)).map(([group, links]) => (
          <div key={group}>
            <div className="eyebrow">{group}</div>
            {links.map((l) => <a key={l.url} href={l.url}>{l.label}</a>)}
          </div>
        ))}
        <div className="foot-note">Read-only. No login. Checks are configured in homelab-ops.</div>
      </footer>

      {selected && <CheckDrawer name={selected} now={now} sites={sites} onClose={() => select(null)} />}
    </div>
  );
}
