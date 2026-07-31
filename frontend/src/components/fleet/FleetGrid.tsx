import { Link } from "react-router";

import { relativeAge, type ClusterState, type FleetState } from "../../lib/fleet";

const CELL: Record<FleetState, string> = {
  critical: "bg-critical/80 hover:bg-critical",
  unreadable: "bg-warning/60 hover:bg-warning",
  warning: "bg-warning/80 hover:bg-warning",
  healthy: "bg-healthy/50 hover:bg-healthy/80",
  neutral: "bg-line hover:bg-ink-3",
  stale: "bg-ink-3/40 hover:bg-ink-3/70",
  unknown: "bg-line/60 hover:bg-line",
};

const GLYPH: Record<FleetState, string> = {
  critical: "●",
  unreadable: "◍",
  warning: "◐",
  healthy: "○",
  neutral: "◌",
  stale: "◌",
  unknown: "·",
};

/**
 * A fleet too large to read as cards.
 *
 * The board is legible at three clusters and useless at three hundred. This is
 * the same data, ordered the same way — worst first — at a density that
 * survives the thousand-cluster target. The glyph carries the state alongside
 * the colour, so it does not depend on hue alone here either.
 */
export function FleetGrid({
  rows,
  onSelect,
}: {
  rows: ClusterState[];
  onSelect: (row: ClusterState) => void;
}) {
  return (
    <ul className="flex flex-wrap gap-1">
      {rows.map((row) => {
        const label = `${row.name} — ${row.state}${
          row.ageMs !== null ? `, ${relativeAge(row.ageMs)}` : ""
        }`;
        const cell = (
          <span
            title={label}
            className={`grid size-6 place-items-center rounded-sm text-[9px] leading-none text-canvas transition-colors duration-fast ${CELL[row.state]}`}
          >
            <span aria-hidden="true">{GLYPH[row.state]}</span>
            <span className="sr-only">{label}</span>
          </span>
        );

        return (
          <li key={row.name}>
            {row.investigationId ? (
              <Link
                to={`/clusters/${encodeURIComponent(row.name)}`}
                className="block rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
              >
                {cell}
              </Link>
            ) : (
              <button
                type="button"
                onClick={() => onSelect(row)}
                className="block rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
              >
                {cell}
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
