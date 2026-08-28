import { useMemo, useState } from "react";

import { EmptyState, Meter, Panel, Tag } from "./ui";
import {
  STATUS_TONE,
  describeCollectionCache,
  filterEvidence,
  formatDuration,
  formatTarget,
  groupEvidenceByKind,
  humanizeKind,
} from "../lib/analysis";
import type { InvestigationResponse } from "../types/investigation";

type InvestigationData = InvestigationResponse["investigation"];

/**
 * Browse every piece of evidence the investigation collected.
 *
 * This is the audit surface: each record shows the exact command that produced
 * it, and degraded records state why they are missing rather than vanishing.
 */
export function EvidenceExplorer({
  investigation,
  citedEvidence = [],
}: {
  investigation?: InvestigationData;
  citedEvidence?: string[];
}) {
  const [query, setQuery] = useState("");
  const [onlyDegraded, setOnlyDegraded] = useState(false);

  const evidence = investigation?.evidence ?? [];
  const coverage = investigation?.evidence_coverage;
  // Null unless reads were actually reused, so this line is absent on a
  // fully-live investigation rather than reading "0 reused" every time.
  const reuse = describeCollectionCache(investigation?.collection_cache);
  const cited = useMemo(() => new Set(citedEvidence), [citedEvidence]);

  const groups = useMemo(
    () => groupEvidenceByKind(filterEvidence(evidence, query, onlyDegraded)),
    [evidence, query, onlyDegraded],
  );

  return (
    <Panel
      title="Evidence Explorer"
      subtitle="Every collected fact, with the command that produced it."
      action={
        coverage ? (
          <div className="flex items-center gap-2">
            <Tag
              label={`${coverage.usable}/${coverage.total} usable`}
              className={
                coverage.completeness === 100
                  ? "border-lime-800 bg-lime-950/40 text-lime-300"
                  : "border-amber-800 bg-amber-950/40 text-amber-300"
              }
            />
          </div>
        ) : undefined
      }
    >
      {evidence.length === 0 ? (
        <EmptyState message="No evidence has been collected yet." />
      ) : (
        <div className="grid gap-4">
          {coverage ? (
            <div>
              <div className="flex items-center justify-between text-xs text-slate-400">
                <span>Evidence completeness</span>
                <span className="font-semibold text-slate-200">
                  {coverage.completeness}%
                </span>
              </div>
              <div className="mt-1">
                <Meter
                  value={coverage.completeness}
                  tone={coverage.completeness === 100 ? "bg-lime-400" : "bg-amber-400"}
                />
              </div>
            </div>
          ) : null}

          {reuse ? (
            <div className="rounded-md border border-slate-800 bg-[#101722] px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <Tag
                  label={reuse.label}
                  className="border-slate-700 bg-slate-900/60 text-slate-300"
                />
                <span className="text-xs text-slate-400">{reuse.detail}</span>
              </div>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-3">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter by kind, target, or command"
              aria-label="Filter evidence"
              className="min-w-0 flex-1 rounded-md border border-slate-700 bg-[#111823] px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-400"
            />
            <label className="flex items-center gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={onlyDegraded}
                onChange={(event) => setOnlyDegraded(event.target.checked)}
                className="size-4 rounded border-slate-700 bg-[#111823]"
              />
              Only gaps
            </label>
          </div>

          {groups.length === 0 ? (
            <EmptyState message="No evidence matches this filter." />
          ) : (
            <div className="grid gap-3">
              {groups.map((group) => (
                <div
                  key={group.kind}
                  className="rounded-md border border-slate-800 bg-[#101722] px-4 py-3"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-slate-200">
                      {humanizeKind(group.kind)}
                    </span>
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[11px] text-slate-600">
                        {group.kind}
                      </span>
                      {group.degraded > 0 ? (
                        <Tag
                          label={`${group.degraded} degraded`}
                          className="border-amber-800 bg-amber-950/40 text-amber-300"
                        />
                      ) : null}
                    </div>
                  </div>

                  <ul className="mt-2 grid gap-2">
                    {group.entries.map((entry) => (
                      <li
                        key={entry.id}
                        className={`rounded-md border px-3 py-2 ${
                          cited.has(entry.id)
                            ? "border-cyan-800 bg-cyan-950/20"
                            : "border-slate-800 bg-[#0d131c]"
                        }`}
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <Tag
                            label={entry.status}
                            className={STATUS_TONE[entry.status]}
                          />
                          <span className="text-xs text-slate-300">
                            {formatTarget(entry.target)}
                          </span>
                          {cited.has(entry.id) ? (
                            <Tag
                              label="cited"
                              className="border-cyan-800 bg-cyan-950/40 text-cyan-300"
                            />
                          ) : null}
                          <span className="ml-auto font-mono text-[11px] text-slate-600">
                            {formatDuration(entry.duration_ms)}
                          </span>
                        </div>

                        {entry.command ? (
                          <pre className="mt-2 overflow-x-auto rounded bg-[#080d14] px-3 py-2 font-mono text-[11px] text-slate-400">
                            {entry.command}
                          </pre>
                        ) : null}

                        {entry.detail ? (
                          <p className="mt-2 text-xs text-amber-200/80">
                            {entry.detail}
                          </p>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
