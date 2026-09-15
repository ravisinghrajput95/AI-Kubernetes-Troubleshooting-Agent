"""Telling a permissions problem apart from a broken cluster.

F6 in `docs/PRODUCTION_READINESS.md` asks for an RBAC preflight — "work is done
before permission failures surface". The preflight it imagines cannot be built
as described: `kubectl auth can-i` is a *command*, and `ResourceRequest` has no
field that can carry one. That closed verb set is the property that makes it
safe to send a request to a customer's cluster, and adding an escape hatch to
improve an error message would be the worst trade in this repository.

What the finding is actually about survives without it. With per-request
impersonation on — the default — the platform reads as the *caller*, so a user
whose Kubernetes RBAC is narrower than the service account's gets an
investigation where every read is refused. The reads themselves are cheap: the
API server answers 403 immediately, so almost nothing is wasted. What was
expensive was the *explanation*. The result read as a degraded investigation of
a broken cluster, and the one fact that would have resolved it in seconds —
"this identity may not read this cluster" — appeared nowhere.

So this is not a preflight. It reads the evidence the collectors already
produced and recognises the shape:

    almost everything FORBIDDEN, almost nothing usable  ->  an access problem

**Status is what makes this possible, and it is provider-agnostic.**
`EvidenceStatus.FORBIDDEN` is recorded by `app/kubernetes/errors.py` for a local
read and by the agent for a remote one, so this works identically through both
without either knowing the check exists. A preflight command would have worked
only on the kubeconfig path.

**One forbidden collector is not an access problem.** A user who can read pods
but not nodes gets a genuinely degraded investigation, which is a normal and
useful outcome; saying "you cannot read this cluster" there would be a lie that
sends someone to their platform team for nothing. The test is dominance, not
presence.
"""

from typing import Any

from app.kubernetes.errors import API_SERVER_UNREACHABLE

# What fraction of the *failures* must have been refusals before a run with no
# usable evidence is called an access problem rather than an unreachable
# cluster. Both conditions are required — see below.
FORBIDDEN_SHARE = 0.6

# Fewest refusals worth diagnosing from. A scope that attempted one read and
# had it refused is technically "nothing usable, all refusals", and saying
# "your RBAC is wrong" from a single data point is a guess wearing a
# conclusion's clothes. Surfaced by a mutation test: excluding
# `not_applicable` made a one-refusal run fire, which is correct arithmetic and
# poor advice.
MINIMUM_REFUSALS = 3


def access_failure(
    coverage: dict[str, Any],
    subject: str = "",
    impersonating: bool = False,
) -> str | None:
    """A message when the identity could not read the cluster, else `None`.

    Two conditions, and the first is the one that keeps this honest:

    - **Nothing usable was collected.** A run with four good reads and six
      refusals is a *partial view*, which the investigation reports honestly
      through `evidence_coverage` and can still reason over. Calling that an
      access problem would send someone to their platform team over an
      investigation that worked.
    - **There were enough refusals to mean something.** One refused read is
      not a diagnosis.
    - **Refusals dominate the failures.** Otherwise a cluster that is simply
      unreachable — every read `UNAVAILABLE` — would be diagnosed as a
      permissions problem, which is the exact confusion this exists to remove,
      pointing the other way.

    An earlier version fired on share alone and produced "Every cluster read
    was refused" for a run where four had succeeded. Its own test caught it.

    `None` covers both "no permissions problem" and "not enough evidence to
    say", deliberately without distinguishing them: a guess here is worse than
    silence, because the generic message is at least not misleading.
    """
    counts = coverage.get("by_status") or {}
    forbidden = int(counts.get("forbidden", 0))
    # Not merely a fast path for `forbidden == 0`: the share test below would
    # reject that anyway. This is the "too little to say" floor.
    if forbidden < MINIMUM_REFUSALS:
        return None

    # `not_applicable` is excluded throughout: an undeployed Prometheus is not
    # a read that was refused, and counting it would make a cluster without
    # optional backends look like a locked one.
    attempted = sum(count for status, count in counts.items() if status != "not_applicable")
    if not attempted:
        return None

    if int(coverage.get("usable", 0)) > 0:
        return None

    failures = attempted
    if forbidden / failures < FORBIDDEN_SHARE:
        return None

    who = f"'{subject}'" if subject else "this identity"
    detail = f"{forbidden} of {failures} reads returned Forbidden"
    if impersonating:
        return (
            f"No cluster read succeeded. Investigations run as the calling user, so "
            f"this is {who}'s Kubernetes RBAC rather than the platform's: {detail}. "
            f"Grant {who} get/list on pods, events, deployments, nodes and services "
            f"in the namespaces you want investigated — or set "
            f"IMPERSONATE_USERS=false to read as the platform's own service account."
        )
    return (
        f"No cluster read succeeded: {detail}. The platform's own credentials lack "
        f"read access to this cluster. Grant get/list on pods, events, deployments, "
        f"nodes and services."
    )


# What fraction of the attempted reads must have timed out before a run with no
# usable evidence is blamed on an agent that stopped answering.
TIMEOUT_SHARE = 0.6


def agent_unanswered(
    coverage: dict[str, Any],
    through_agent: bool,
    cluster_id: str = "",
) -> str | None:
    """A message when a cluster reached through its agent answered nothing.

    The locked door's sibling. Freezing an agent with SIGSTOP — its stream stays
    open, nothing replies — produced an investigation whose ten reads all
    recorded `timeout` and whose explanation was the generic one: "Verify
    kubeconfig, cluster access, and kubectl permissions." A cluster reached
    through an agent involves no kubeconfig at all, so that sentence sends an
    operator to check configuration that is not in the path — the same wrong
    instruction M8a's refusal already rewrites for an agent held elsewhere.

    Same three conditions as `access_failure`, for the same reasons: nothing
    usable, enough timeouts to diagnose from, and timeouts dominating the
    failures. And one more that is the point: the reads went through an agent.
    A kubeconfig path that timed out is a slow API server, and the generic
    message is at least not wrong there.
    """
    if not through_agent:
        return None
    counts = coverage.get("by_status") or {}
    timeouts = int(counts.get("timeout", 0))
    if timeouts < MINIMUM_REFUSALS:
        return None
    attempted = sum(count for status, count in counts.items() if status != "not_applicable")
    if not attempted or int(coverage.get("usable", 0)) > 0:
        return None
    if timeouts / attempted < TIMEOUT_SHARE:
        return None

    who = f"'{cluster_id}'" if cluster_id else "this cluster"
    return (
        f"No read came back from the agent for {who}: {timeouts} of {attempted} reads "
        f"timed out. The agent is connected but not answering, so this is not a "
        f"kubeconfig or permissions problem — check the k8s-ops-agent pod in that "
        f"cluster, which may be hung, starved of CPU, or unable to reach its own "
        f"API server."
    )


def agent_cannot_reach_api(
    coverage: dict[str, Any],
    through_agent: bool,
    cluster_id: str = "",
) -> str | None:
    """A message when an agent answered, but its API server did not.

    The third of these. With the agent connected and heartbeating, and only its
    traffic to port 6443 dropped, every read came back promptly as a failure
    — so not `agent_unanswered`, which is about silence — and the investigation
    said "Verify kubeconfig, cluster access, and kubectl permissions". Nothing
    about a kubeconfig was involved: the agent's own path to its API server was
    broken, and each record now says so in the agent's words.

    Same guards as its siblings: through an agent, nothing usable, enough
    failures to diagnose from, and those failures dominated by the network.
    """
    if not through_agent:
        return None
    degraded = coverage.get("degraded") or []
    unreachable = [
        item for item in degraded if str(item.get("detail", "")).startswith(API_SERVER_UNREACHABLE)
    ]
    if len(unreachable) < MINIMUM_REFUSALS or int(coverage.get("usable", 0)) > 0:
        return None
    attempted = [item for item in degraded if item.get("status") != "not_applicable"]
    if not attempted or len(unreachable) / len(attempted) < TIMEOUT_SHARE:
        return None

    who = f"'{cluster_id}'" if cluster_id else "this cluster"
    reason = str(unreachable[0]["detail"])[len(API_SERVER_UNREACHABLE) + 2 :]
    return (
        f"The agent for {who} is connected and answering, but it could not reach its "
        f"cluster's API server: {len(unreachable)} of {len(attempted)} reads failed "
        f"({reason}). This is the network path from the agent to its API server, not "
        f"the platform's kubeconfig or your permissions."
    )
