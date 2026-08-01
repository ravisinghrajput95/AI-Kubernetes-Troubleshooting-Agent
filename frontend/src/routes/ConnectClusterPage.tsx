import { useState } from "react";
import { Link } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { AgentDot } from "../components/fleet/AgentDot";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { ApiError, createEnrolment, getAgents, type Enrolment } from "../services/api";

/**
 * Connect a cluster.
 *
 * The console had no answer to "add a cluster" at all: the agent existed, the
 * gateway accepted it, and the only route in was a CLI on the platform host
 * plus a binary you built yourself. This is that route, made usable.
 *
 * What it deliberately does not do is hide what it is handing over. The token
 * is single-use, short-lived, and shown once — the platform keeps only its
 * digest and cannot show it again — and the manifest is displayed in full
 * rather than behind a download, because an operator is about to run it with
 * cluster-admin and is entitled to read it first.
 */
export function ConnectClusterPage() {
  useDocumentTitle("Connect a cluster");
  const queryClient = useQueryClient();

  const [clusterId, setClusterId] = useState("");
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: getAgents,
    // While the page is open, an agent that connects should appear without a
    // reload — this page is the one place someone is actively waiting for it.
    refetchInterval: 5000,
  });

  const connected = agents.data?.items.find((item) => item.cluster_id === enrolment?.cluster_id);

  async function mint() {
    const name = clusterId.trim();
    if (!name || pending) {
      return;
    }
    setPending(true);
    setError("");
    try {
      setEnrolment(await createEnrolment(name));
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : "Could not create an enrolment token.",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="mx-auto max-w-document px-6 py-8">
      <h1 className="text-display">Connect a cluster</h1>
      <p className="mt-1 max-w-measure text-sm leading-6 text-ink-2">
        An agent runs inside your cluster and dials out to this platform. No
        inbound port is opened, and it can only read — the role it runs under
        grants <code className="font-mono text-sm">get</code>,{" "}
        <code className="font-mono text-sm">list</code> and{" "}
        <code className="font-mono text-sm">watch</code>, and the agent refuses
        any request that is not one of the evidence kinds it knows.
      </p>

      {!agents.data?.gateway_enabled ? (
        <section className="mt-6 rounded-lg border border-warning/40 bg-warning/5 p-4">
          <h2 className="text-h2 text-warning">No agent gateway is running</h2>
          <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
            Set <code className="font-mono text-sm">AGENT_GATEWAY_PORT</code> on
            the backend and restart it. Until then clusters can only be reached
            with the platform&apos;s own kubeconfig.
          </p>
        </section>
      ) : null}

      <section className="mt-6 rounded-lg border border-line bg-surface p-4">
        <h2 className="text-h2">1 · Name the cluster</h2>
        <p className="mt-1 max-w-measure text-sm leading-6 text-ink-2">
          This is the identity the certificate will carry. It cannot be changed
          afterwards without re-enrolling, and the agent cannot override it.
        </p>

        <div className="mt-4 flex flex-wrap items-end gap-3">
          <div className="min-w-64 flex-1">
            <label htmlFor="cluster-id" className="text-sm text-ink-2">
              Cluster id
            </label>
            <input
              id="cluster-id"
              value={clusterId}
              onChange={(event) => setClusterId(event.target.value)}
              placeholder="prod-eu-west-1"
              className="mt-1.5 w-full rounded-md border border-line bg-raised px-3 py-2 font-mono text-sm text-ink outline-none transition-colors duration-fast placeholder:text-ink-3 focus:border-info focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-info"
            />
          </div>
          <button
            type="button"
            onClick={() => void mint()}
            disabled={pending || !clusterId.trim()}
            className="rounded-md border border-info/40 bg-info/10 px-4 py-2 text-sm font-medium text-info transition-colors duration-fast hover:bg-info/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info disabled:cursor-not-allowed disabled:border-line disabled:bg-raised disabled:text-ink-3"
          >
            {pending ? "Creating…" : "Create enrolment"}
          </button>
        </div>

        {error ? (
          <p role="alert" className="mt-3 text-sm text-critical">
            {error}
          </p>
        ) : null}
      </section>

      {enrolment ? (
        <>
          <section className="mt-4 rounded-lg border border-line bg-surface p-4">
            <h2 className="text-h2">2 · Install the agent</h2>
            <p className="mt-1 max-w-measure text-sm leading-6 text-ink-2">
              Apply this to{" "}
              <span className="font-mono text-sm text-ink">{enrolment.cluster_id}</span>. The
              token inside is single-use and expires in{" "}
              {enrolment.expires_in_minutes} minutes; it is shown here once and
              cannot be retrieved again.
            </p>

            <Copyable
              label="Kubernetes manifest"
              filename={`${enrolment.cluster_id}-agent.yaml`}
              value={enrolment.manifest}
            />

            <details className="mt-4 rounded-md border border-line-muted bg-raised/50 p-3">
              <summary className="cursor-pointer text-sm text-ink-2">
                Run it outside a cluster instead
              </summary>
              <p className="mt-2 max-w-measure text-sm leading-6 text-ink-3">
                For a cluster you already have a kubeconfig for, or to try it
                from a laptop.
              </p>
              <Copyable label="Docker" value={enrolment.docker_command} />
            </details>
          </section>

          <section className="mt-4 rounded-lg border border-line bg-surface p-4">
            <h2 className="text-h2">3 · Wait for it to check in</h2>
            <div className="mt-3 flex items-center gap-3">
              {connected ? (
                <>
                  <AgentDot agent={connected} />
                  <span className="text-sm text-ink-2">
                    {enrolment.cluster_id} is connected and answering.
                  </span>
                  <Link
                    to="/"
                    className="ml-auto text-sm text-info underline-offset-4 hover:underline"
                  >
                    Open the fleet
                  </Link>
                </>
              ) : (
                <>
                  <span
                    aria-hidden="true"
                    className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-ink-3"
                  />
                  <span className="text-sm text-ink-2">
                    Waiting for the agent to dial in…
                  </span>
                </>
              )}
            </div>
          </section>
        </>
      ) : null}

      <ConnectedAgents />
    </div>
  );
}

function ConnectedAgents() {
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: getAgents,
    refetchInterval: 10_000,
  });

  const items = agents.data?.items ?? [];
  if (items.length === 0) {
    return null;
  }

  return (
    <section className="mt-8">
      <h2 className="text-h2">Connected agents</h2>
      <p className="mt-1 max-w-measure text-sm text-ink-2">
        Agents attached to the worker that answered this request. A fleet spread
        across several workers shows only its own until requests are routed by
        stream ownership.
      </p>
      <ul className="mt-3 grid gap-2">
        {items.map((agent) => (
          <li
            key={agent.cluster_id}
            className="rounded-lg border border-line bg-surface px-4 py-3"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <span className="font-mono text-sm text-ink">{agent.cluster_id}</span>
              <AgentDot agent={agent} />
            </div>
            <p className="mt-1 text-sm text-ink-3">
              agent {agent.agent_version || "unknown"} · Kubernetes{" "}
              {agent.kubernetes_version || "unknown"} · {agent.supported_kinds.length} evidence
              kinds
              {agent.identity_source === "certificate"
                ? " · identity verified by certificate"
                : " · identity declared, not verified"}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A block of text with a copy button, and a download when it is a file. */
function Copyable({
  label,
  value,
  filename,
}: {
  label: string;
  value: string;
  filename?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be refused; the text is on screen either way.
      setCopied(false);
    }
  }

  function download() {
    const blob = new Blob([value], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename ?? "agent.yaml";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="mt-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm text-ink-2">{label}</span>
        <span className="flex gap-2">
          <button
            type="button"
            onClick={() => void copy()}
            className="rounded-md border border-line bg-raised px-2.5 py-1 text-xs text-ink-2 transition-colors duration-fast hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
          >
            {copied ? "Copied" : "Copy"}
          </button>
          {filename ? (
            <button
              type="button"
              onClick={download}
              className="rounded-md border border-line bg-raised px-2.5 py-1 text-xs text-ink-2 transition-colors duration-fast hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
            >
              Download
            </button>
          ) : null}
        </span>
      </div>
      <pre className="mt-1.5 max-h-80 overflow-auto rounded-md border border-line-muted bg-base px-3 py-2 font-mono text-xs leading-5 text-ink-2">
        {value}
      </pre>
    </div>
  );
}
