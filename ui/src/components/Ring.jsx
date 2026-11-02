import { ringOrder } from "../lib.js";

// The mark: one tick per check, round the eye. Each site is one arc, with a gap
// between. Down ticks are long and red, so a problem reads at
// a glance from across the room. Paused ones are short and grey.
export default function Ring({ checks, siteOrder, size = 320, onPick }) {
  const ordered = ringOrder(checks, siteOrder);
  const gap = 2; // empty slots between the two sites' arcs
  const sites = [...new Set(ordered.map((c) => c.site))];
  const slots = ordered.length + gap * sites.length || 1;
  const cx = 100, cy = 100;
  let slot = 0, lastSite = null;
  const ticks = ordered.map((c) => {
    if (c.site !== lastSite) {
      slot += lastSite === null ? gap / 2 : gap;
      lastSite = c.site;
    }
    const angle = (slot++ / slots) * 2 * Math.PI - Math.PI / 2;
    const r1 = 62, r2 = c.status === "down" ? 96 : c.status === "failing" ? 90 : c.status === "paused" ? 76 : 84;
    return { c, x1: cx + r1 * Math.cos(angle), y1: cy + r1 * Math.sin(angle),
      x2: cx + r2 * Math.cos(angle), y2: cy + r2 * Math.sin(angle) };
  });
  return (
    <svg className="ring" viewBox="0 0 200 200" width={size} height={size} role="img"
         aria-label={`${checks.length} checks`}>
      <circle cx={cx} cy={cy} r="54" className="ring-inner" />
      {ticks.map(({ c, x1, y1, x2, y2 }) => (
        <line key={c.name} x1={x1} y1={y1} x2={x2} y2={y2}
              className={`tick tick-${c.status}`} onClick={() => onPick?.(c.name)}>
          <title>{`${c.name}: ${c.status}`}</title>
        </line>
      ))}
    </svg>
  );
}
