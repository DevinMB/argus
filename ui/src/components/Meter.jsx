// A thin percentage ring with its numbers beside it.
export default function Meter({ label, sub, value, total, tone = "up" }) {
  const pct = total ? value / total : 0;
  const r = 15.9155; // circumference 100
  return (
    <div className="meter">
      <svg viewBox="0 0 36 36" className="meter-ring" aria-hidden="true">
        <circle cx="18" cy="18" r={r} className="meter-track" />
        <circle cx="18" cy="18" r={r} className={`meter-arc tone-${tone}`}
                strokeDasharray={`${pct * 100} ${100 - pct * 100}`} strokeDashoffset="25" />
      </svg>
      <div className="meter-text">
        <div className="meter-label">{label}</div>
        <div className="meter-value">{total ? `${value} / ${total}` : "none"}</div>
        {sub && <div className="meter-sub">{sub}</div>}
      </div>
    </div>
  );
}
