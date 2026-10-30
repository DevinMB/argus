import { useState } from "react";
import { joinList } from "../lib.js";

const KINDS = {
  http: ["HTTP", "asks the page for a response and expects the right status code, usually 200. Redirects count as a failure, so a login page cannot pass for the service."],
  tcp: ["TCP", "opens a connection to the port. Used where there is no web page to ask, like Kafka."],
  ping: ["Ping", "sends an ICMP echo and waits for the reply. Only for hosts with nothing else to check."],
};

function HowItChecks({ checks, heartbeats }) {
  const kinds = {};
  for (const c of checks) kinds[c.kind] = (kinds[c.kind] ?? 0) + 1;
  const intervals = joinList([...new Set(checks.map((c) => `${c.interval} s`))]);
  const timeouts = [...new Set(checks.map((c) => c.timeout))].sort((a, b) => a - b);
  const timeout = timeouts.length > 1 ? `${timeouts[0]} to ${timeouts.at(-1)} s` : `${timeouts[0]} s`;
  const critical = checks.filter((c) => c.severity === "critical").length;
  return (
    <>
      <ul className="how-kinds">
        {Object.entries(kinds).map(([k, n]) => (
          <li key={k}>
            <span className="chip">{KINDS[k]?.[0] ?? k}</span>
            <span><strong>{n}</strong> {KINDS[k]?.[1] ?? ""}</span>
          </li>
        ))}
      </ul>
      <dl className="how">
        <dt>Timing</dt>
        <dd>Every check runs every {intervals}, staggered across the minute, with a {timeout} timeout.</dd>
        <dt>Down</dt>
        <dd>One failure is a blip (<span className="st-failing">failing</span>). Three in a row and it is <span className="st-down">down</span>. One success and it is up again.</dd>
        <dt>Paging</dt>
        <dd>{critical} checks are marked <span className="chip chip-critical">pages</span>: if one goes down, Prometheus sends it to Telegram. The rest show here and on the boards only, because something else already pages for them.</dd>
        <dt>Watching Argus</dt>
        <dd>The other site watches this one. If Argus stops or hangs, you get paged from there.</dd>
        <dt>Heartbeats</dt>
        <dd>
          Timers report in after each run; one that goes quiet past its limit is late.{" "}
          {heartbeats.length
            ? `${heartbeats.length} ${heartbeats.length === 1 ? "timer reports" : "timers report"} today.`
            : "None yet."}
        </dd>
      </dl>
    </>
  );
}

function WhyArgus() {
  return (
    <>
      <p>
        In Greek myth, Argus Panoptes was a giant with a hundred eyes. Hera set him to keep watch,
        and he was the perfect guard because he never slept all at once: a few eyes always stayed
        open while the rest rested.
      </p>
      <p>
        That is the job here. Every tick on the ring is one of his eyes, one per service, and the
        checks take turns through the minute so something is always looking.
      </p>
      <p className="why-coda">
        When Hermes finally lulled him to sleep, Hera set his eyes in the peacock&apos;s tail. We are
        hoping for a quieter retirement.
      </p>
    </>
  );
}

// Pills under the hero text; one panel open at a time, below the row.
export default function Asides({ checks, heartbeats }) {
  const [open, setOpen] = useState(null);
  const panels = [
    ["how", "How it checks", <HowItChecks checks={checks} heartbeats={heartbeats} />],
    ["why", "Why Argus?", <WhyArgus />],
  ];
  return (
    <div className="asides">
      <div className="pills">
        {panels.map(([id, label]) => (
          <button key={id} className={`pill pill-${id}`} aria-expanded={open === id}
                  aria-controls={`aside-${id}`} onClick={() => setOpen(open === id ? null : id)}>
            {label}
          </button>
        ))}
      </div>
      {panels.map(([id, , body]) => open === id && (
        <div key={id} id={`aside-${id}`} className="aside-body">{body}</div>
      ))}
    </div>
  );
}
