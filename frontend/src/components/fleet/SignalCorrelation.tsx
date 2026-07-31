import { SeverityDot } from "../report/SeverityDot";
import { severityTone } from "../../lib/report";
import type { SignalCluster } from "../../lib/fleet";

/**
 * The same failure across more than one cluster.
 *
 * Eight clusters failing the same image pull is one incident, not eight, and
 * no single investigation can see that. Derived by grouping stored signals on
 * their stable type prefix — a query, not a model call.
 */
export function SignalCorrelation({
  groups,
  limited,
}: {
  groups: SignalCluster[];
  limited: boolean;
}) {
  if (groups.length === 0) {
    return null;
  }

  return (
    <section className="mt-8">
      <h2 className="text-h2">Across the fleet</h2>
      <p className="mt-1 text-sm text-ink-2">
        The same finding on more than one cluster.
      </p>

      <ul className="mt-4 grid gap-2">
        {groups.map((group) => (
          <li key={group.type} className="rounded-lg border border-line bg-surface p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <span className="flex items-baseline gap-2">
                <SeverityDot tone={severityTone(group.severity)} />
                <span className="font-mono text-sm text-ink">{group.type}</span>
              </span>
              <span className="font-mono text-sm text-ink-2">
                {group.clusters.length} clusters
              </span>
            </div>
            <p className="mt-2 text-sm text-ink-2">{group.summary}</p>
            <p className="mt-1 truncate font-mono text-sm text-ink-3">
              {group.clusters.join(", ")}
            </p>
          </li>
        ))}
      </ul>

      {limited ? (
        <p className="mt-3 text-sm text-ink-3">
          Correlated across the most recently investigated clusters only. A
          fleet-wide figure needs an aggregate endpoint rather than one report
          fetch per cluster.
        </p>
      ) : null}
    </section>
  );
}
