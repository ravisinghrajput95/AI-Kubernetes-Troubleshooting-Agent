import { CitationChip } from "../report/CitationChip";
import { SeverityDot } from "../report/SeverityDot";
import {
  clusterOverview,
  evidenceIdForKind,
  securityWarnings,
  topConsumers,
} from "../../lib/cluster";
import { evidenceIndex } from "../../lib/report";
import type { InvestigationResponse } from "../../types/investigation";

type Investigation = InvestigationResponse["investigation"];

/**
 * What the last investigation established about this cluster.
 *
 * Six of the twelve tabs the brief asked for arrive here as sections — the
 * information is delivered, the browser affordance is not. Every figure is
 * citable; if it cannot be traced to a record, it does not belong on the page.
 */
export function ClusterOverview({
  investigation,
  selectedEvidence,
  onSelectEvidence,
}: {
  investigation?: Investigation;
  selectedEvidence: string;
  onSelectEvidence: (id: string) => void;
}) {
  const groups = clusterOverview(investigation);
  const index = evidenceIndex(investigation);
  const consumers = topConsumers(investigation);
  const warnings = securityWarnings(investigation);

  if (groups.length === 0) {
    return (
      <p className="max-w-measure text-sm leading-6 text-ink-2">
        This investigation did not establish anything about the cluster as a
        whole. Its evidence and gaps are still on the Evidence tab.
      </p>
    );
  }

  return (
    <div className="grid gap-8">
      <div className="grid gap-x-10 gap-y-6 sm:grid-cols-2 lg:grid-cols-3">
        {groups.map((group) => (
          <section key={group.title}>
            <h3 className="text-label uppercase text-ink-3">{group.title}</h3>
            <dl className="mt-3 grid gap-2">
              {group.figures.map((figure) => {
                const evidenceId = figure.kind
                  ? evidenceIdForKind(investigation, figure.kind)
                  : undefined;
                return (
                  <div key={figure.label} className="flex items-baseline justify-between gap-3">
                    <dt className="text-sm text-ink-3">{figure.label}</dt>
                    <dd className="flex items-baseline gap-1.5 text-right">
                      {figure.tone ? (
                        <SeverityDot tone={figure.tone} label={figure.value} />
                      ) : (
                        <span className="font-mono text-sm text-ink">{figure.value}</span>
                      )}
                      {evidenceId ? (
                        <CitationChip
                          evidence={index.get(evidenceId)}
                          active={evidenceId === selectedEvidence}
                          onSelect={() => onSelectEvidence(evidenceId)}
                        />
                      ) : null}
                    </dd>
                  </div>
                );
              })}
            </dl>
          </section>
        ))}
      </div>

      {warnings.length > 0 ? (
        <section>
          <h3 className="text-label uppercase text-ink-3">Security warnings</h3>
          <ul className="mt-3 grid gap-2">
            {warnings.map((warning) => (
              <li key={warning.label} className="flex flex-wrap items-baseline gap-x-2">
                <SeverityDot tone="warning" label={warning.label} />
                <span className="text-sm text-ink-2">{warning.detail}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {consumers.length > 0 ? (
        <section>
          <h3 className="text-label uppercase text-ink-3">Top consumers</h3>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[420px] border-collapse text-left">
              <thead>
                <tr>
                  {["Pod", "CPU", "Memory"].map((header) => (
                    <th
                      key={header}
                      scope="col"
                      className="border-b border-line pb-2 pr-4 text-label uppercase text-ink-3"
                    >
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {consumers.map((pod) => (
                  <tr
                    key={`${pod.namespace}/${pod.name}`}
                    className="border-b border-line-muted"
                  >
                    <td className="py-2 pr-4 font-mono text-sm text-ink-2">
                      {pod.namespace}/{pod.name}
                    </td>
                    <td className="py-2 pr-4 font-mono text-sm tabular-nums text-ink-2">
                      {pod.cpu}
                    </td>
                    <td className="py-2 font-mono text-sm tabular-nums text-ink-2">
                      {pod.memory}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  );
}
