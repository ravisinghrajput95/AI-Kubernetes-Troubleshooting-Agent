"""The agent timings, in one place because their *order* is the design.

    AGENT_HEARTBEAT_SECONDS < AGENT_STALE_SECONDS < PRESENCE_TTL_SECONDS < UNCLAIMED_GRACE_SECONDS
            15                        30                      45                    60

Each inequality protects something different, and two of them were only ever
stated in separate files:

- **heartbeat < stale**: one missed heartbeat is a slow network; two is an agent
  that is not answering.
- **stale < presence TTL**: this one was missing. Both were 45, so a presence
  record expired at the exact moment it would have read "silent" — the console's
  red "Agent silent for Ns" state could never appear on a multi-worker
  deployment. Measured against an agent frozen with SIGSTOP: `/agents` reported
  "online, seen 0s ago" for 43 seconds and then the agent simply vanished. The
  gap between the two is now the window in which a hung agent is shown as what
  it is.
- **presence TTL < unclaimed grace** (`app/jobs/consumer.py`): what makes
  routing recovery terminate rather than loop. A job routed to a worker that
  dies is re-offered after the grace; if presence outlived it, the re-offer
  would route straight back to the dead worker, forever.

`tests/test_agent_routing.py` asserts the whole chain. No module imports grpc,
because presence is installed on workers that run no gateway (F21).
"""

# How often the platform pings a connected agent.
AGENT_HEARTBEAT_SECONDS = 15.0

# How long silence may last before an agent stops counting as online: two
# missed heartbeats.
AGENT_STALE_SECONDS = 30.0

# How long a presence record survives without a heartbeat refreshing it. Three
# heartbeats: long enough to show a silent agent as silent for a heartbeat's
# worth of time, short enough that a dead worker's agents lapse before routing
# recovery re-offers their jobs.
PRESENCE_TTL_SECONDS = 45
