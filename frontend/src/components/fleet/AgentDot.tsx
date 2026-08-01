import type { AgentStatus } from "../../types/investigation";

/**
 * Whether a cluster's agent is answering.
 *
 * Three states, not two. "Connected" and "not connected" would be a lie in the
 * middle: an agent whose stream is open but which has not answered a heartbeat
 * is neither healthy nor gone, and it is exactly the state an operator needs to
 * see before they conclude a cluster is fine.
 *
 * Colour is never the only signal — the label carries the same information, so
 * this reads the same to someone who cannot distinguish the two greens.
 */
export function AgentDot({
  agent,
  label = true,
}: {
  agent: AgentStatus | null | undefined;
  label?: boolean;
}) {
  if (!agent) {
    return null;
  }

  const stale = !agent.online;
  const degraded = Boolean(agent.degradation);
  // `local === false` only ever appears in a multi-replica deployment, where
  // the agent is connected to a different pod. It is online; this worker just
  // cannot collect through it yet.
  const elsewhere = agent.local === false;

  const tone = stale ? "bg-critical" : degraded ? "bg-warning" : "bg-healthy";
  const text = stale
    ? `Agent silent for ${Math.round(agent.seconds_since_seen)}s`
    : degraded
      ? "Agent degraded"
      : elsewhere
        ? "Agent online, on another worker"
        : "Agent online";

  return (
    <span
      className="inline-flex items-center gap-1.5"
      title={
        degraded
          ? agent.degradation
          : elsewhere
            ? `Connected to worker ${agent.worker}; investigations are routed per worker until fleet routing lands.`
            : `Last heard from ${new Date(agent.last_seen).toLocaleTimeString()}`
      }
    >
      <span
        aria-hidden="true"
        className={`h-2 w-2 shrink-0 rounded-full ${tone} ${
          stale ? "" : "shadow-[0_0_0_3px_rgba(52,199,123,0.15)]"
        }`}
      />
      {label ? <span className="text-sm text-ink-2">{text}</span> : <span className="sr-only">{text}</span>}
    </span>
  );
}
