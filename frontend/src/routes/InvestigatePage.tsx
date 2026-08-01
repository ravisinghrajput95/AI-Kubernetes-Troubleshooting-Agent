import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { AgentDot } from "../components/fleet/AgentDot";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { useScope } from "../hooks/useScope";
import {
  getInvestigationHistory,
  getKubernetesContexts,
  startInvestigationJob,
} from "../services/api";

/**
 * Start an investigation.
 *
 * This page predated the console redesign and was the last one still wearing
 * the old skin — a saturated cyan call to action, a "Multi-Cluster Dashboard"
 * panel that restated the fleet, and hardcoded slate colours next to pages
 * built on the design tokens. It said "Investigate Kubernetes Cluster" above a
 * form whose only job was to pick a scope.
 *
 * What it is now: the scope, and the button. The fleet lives on the fleet page
 * and is linked rather than duplicated, and recent runs are a list of
 * addresses rather than a second history table.
 */
export function InvestigatePage() {
  useDocumentTitle("Investigations");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Scope lives in the URL so it is shareable and survives a reload.
  const { cluster: selectedContext, setCluster: setSelectedContext } = useScope();
  const [namespace, setNamespace] = useState("");
  const [kind, setKind] = useState("");
  const [name, setName] = useState("");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const contexts = useQuery({
    queryKey: ["kubernetes-contexts"],
    queryFn: getKubernetesContexts,
  });

  const clusters = contexts.data?.items ?? [];
  const selected = clusters.find((item) => item.name === selectedContext);

  /**
   * Submitting is navigation.
   *
   * The run gets an address the moment the backend accepts it, so it can be
   * shared while it is still collecting. Progress and result render there.
   */
  async function start() {
    if (!selectedContext || starting) {
      return;
    }
    setStarting(true);
    setError("");
    try {
      const accepted = await startInvestigationJob(selectedContext, {
        namespace: namespace.trim() || undefined,
        resource_kind: kind || undefined,
        resource_name: name.trim() || undefined,
      });
      queryClient.invalidateQueries({ queryKey: ["investigation-history"] });
      navigate(`/investigations/${accepted.id}`);
    } catch {
      setError("Unable to start the investigation. Confirm the backend API is reachable.");
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="mx-auto max-w-document px-6 py-8">
      <h1 className="text-display">Investigate</h1>
      <p className="mt-1 max-w-measure text-sm leading-6 text-ink-2">
        Evidence is collected read-only and nothing is ever applied. Narrow the
        scope to make a large cluster faster to read — leaving it empty
        investigates everything.
      </p>

      <section className="mt-6 rounded-lg border border-line bg-surface p-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Cluster" htmlFor="cluster">
            <select
              id="cluster"
              value={selectedContext}
              onChange={(event) => setSelectedContext(event.target.value)}
              className={INPUT}
            >
              <option value="">Select a cluster…</option>
              {clusters.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                  {item.connection === "agent" ? " (agent)" : ""}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Namespace" htmlFor="namespace" hint="Optional">
            <input
              id="namespace"
              value={namespace}
              onChange={(event) => setNamespace(event.target.value)}
              placeholder="All namespaces"
              className={INPUT}
            />
          </Field>

          <Field label="Resource kind" htmlFor="kind" hint="Optional">
            <select
              id="kind"
              value={kind}
              onChange={(event) => setKind(event.target.value)}
              className={INPUT}
            >
              <option value="">Whole cluster</option>
              <option value="pod">Pod</option>
              <option value="deployment">Deployment</option>
            </select>
          </Field>

          <Field
            label="Resource name"
            htmlFor="name"
            hint={kind ? "Required" : "Pick a kind first"}
          >
            <input
              id="name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={kind ? `${kind} name` : "—"}
              disabled={!kind}
              className={`${INPUT} disabled:cursor-not-allowed disabled:text-ink-3`}
            />
          </Field>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line-muted pt-4">
          <button
            type="button"
            onClick={() => void start()}
            disabled={starting || !selectedContext}
            className="rounded-md border border-info/40 bg-info/10 px-4 py-2 text-sm font-medium text-info transition-colors duration-fast hover:bg-info/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info disabled:cursor-not-allowed disabled:border-line disabled:bg-raised disabled:text-ink-3"
          >
            {starting ? "Starting…" : "Start investigation"}
          </button>

          {selected?.agent ? (
            <span className="flex items-center gap-2 text-sm text-ink-2">
              <AgentDot agent={selected.agent} />
              Collected through the agent in this cluster.
            </span>
          ) : selectedContext ? (
            <span className="text-sm text-ink-3">
              Collected with the platform&apos;s kubeconfig.
            </span>
          ) : null}

          <Link
            to="/"
            className="ml-auto text-sm text-ink-3 underline-offset-4 transition-colors duration-fast hover:text-ink-2 hover:underline"
          >
            See the whole fleet
          </Link>
        </div>
      </section>

      {error ? (
        <p
          role="alert"
          className="mt-4 rounded-md border border-critical/40 bg-critical/5 px-4 py-3 text-sm text-critical"
        >
          {error}
        </p>
      ) : null}

      <RecentInvestigations />
    </div>
  );
}

const INPUT =
  "w-full rounded-md border border-line bg-raised px-3 py-2 text-sm text-ink outline-none transition-colors duration-fast placeholder:text-ink-3 focus:border-info focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-info";

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label htmlFor={htmlFor} className="flex items-baseline justify-between gap-2">
        <span className="text-sm text-ink-2">{label}</span>
        {hint ? <span className="text-xs text-ink-3">{hint}</span> : null}
      </label>
      <div className="mt-1.5">{children}</div>
    </div>
  );
}

/** Recent runs, as links. The full table lives on /reports. */
function RecentInvestigations() {
  const { data = [] } = useQuery({
    queryKey: ["investigation-history"],
    queryFn: getInvestigationHistory,
  });

  if (data.length === 0) {
    return null;
  }

  return (
    <section className="mt-8">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-h2">Recent</h2>
        <Link
          to="/reports"
          className="text-sm text-ink-3 underline-offset-4 transition-colors duration-fast hover:text-ink-2 hover:underline"
        >
          All reports
        </Link>
      </div>
      <ul className="mt-3 grid gap-1">
        {data.slice(0, 6).map((item) => (
          <li key={item.id}>
            <Link
              to={`/investigations/${item.id}`}
              className="flex items-baseline justify-between gap-4 rounded-md border border-line bg-surface px-4 py-3 text-sm transition-colors duration-fast hover:border-line-strong focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
            >
              <span className="min-w-0 flex-1 truncate text-ink">{item.root_cause}</span>
              <span className="shrink-0 font-mono text-sm text-ink-3">
                {item.context || item.namespace} · {item.confidence}%
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
