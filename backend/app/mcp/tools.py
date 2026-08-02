"""The platform's capabilities, as tools a customer's own agent can call.

§3.7: "Exposing the platform's evidence and investigation capability as MCP
tools lets a customer's own AI agents consume it. That is a valuable product
surface and a poor fleet transport (§7)."

**The danger in a second entry point is that it is a second entry point.**
M6.5 made authorisation impossible to forget for HTTP by putting one check in a
router-level dependency and denying any route absent from `ROUTE_PERMISSIONS`.
A tool call is not a route, so none of that machinery applies to it — and an
MCP surface that reached `run_investigation` directly would be a complete
authorisation bypass wearing a different protocol.

So this table is the same idea in the same shape, with the same default:

    **a tool with no entry here cannot be called.**

`tests/test_mcp.py` derives the tool list from the registry and asserts every
one has a permission, so adding a tool without deciding what it requires fails
a test *and* fails closed. Costed tools are the ones needing
`investigation.run`, which is also what makes them rate limited — the same
`COSTED_PERMISSIONS` set, not a second list.

What is deliberately **not** exposed: anything that mutates the fleet. No
enrolment, no revocation, no member management. Those need `admin`, they are
the operations M6.5 identified as destructive, and handing them to an
autonomous agent is a decision a customer should make explicitly through the
HTTP API rather than inherit from adopting a tool server.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.authz.models import Permission

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "ai-kubernetes-agent"


@dataclass(frozen=True, slots=True)
class Tool:
    """One capability, its schema, and what a caller must hold to use it."""

    name: str
    description: str
    permission: Permission
    schema: dict[str, Any]
    handler: Callable[..., Any]

    def describe(self) -> dict[str, Any]:
        """The `tools/list` shape."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


def _cluster_arg(required: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "cluster": {
                "type": "string",
                "description": "Cluster name, as reported by list_clusters.",
            }
        },
        "additionalProperties": False,
    }
    if required:
        schema["required"] = ["cluster"]
    return schema


async def list_clusters(principal, **_: Any) -> dict[str, Any]:
    """Every cluster this caller's tenant can reach."""
    from app.api.investigate import merge_agent_clusters
    from app.kubernetes.context_service import KubernetesContextService

    result = KubernetesContextService(principal=principal).list_contexts()
    items = merge_agent_clusters(result.get("items", []))
    return {
        "clusters": [
            {
                "name": item.get("name", ""),
                "connection": item.get("connection", ""),
                "reachable": bool(item.get("agent")) or item.get("connection") == "kubeconfig",
            }
            for item in items
        ]
    }


async def start_investigation(
    principal, cluster: str = "", namespace: str = "", **_: Any
) -> dict[str, Any]:
    """Submit an investigation and return its id, without waiting for it.

    Deliberately not blocking. An investigation takes seconds to minutes, and a
    tool call that occupied an agent's turn for that long would push callers
    toward timeouts and retries — which, on the one operation that reads a
    production cluster, is the last place to invite a retry.
    """
    from app.jobs.runner import get_job_runner
    from app.models.investigation import InvestigationRequest

    job = get_job_runner().submit(
        InvestigationRequest(context=cluster or None, namespace=namespace or None),
        principal=principal,
    )
    return {
        "investigation_id": job.id,
        "status": str(job.status),
        "note": "Poll get_investigation for the diagnosis; this returns immediately.",
    }


async def get_investigation(principal, investigation_id: str = "", **_: Any) -> dict[str, Any]:
    """Status and, once finished, the diagnosis.

    The *diagnosis*, not the investigation: the stored result is megabytes of
    cluster interior and an agent asking "what is wrong" wants the conclusion.
    The same reasoning as `app/notify` — an allowlist, so a future collector
    cannot widen what this returns.
    """
    from app.api.investigate import _visible_owner
    from app.jobs.store import get_job_store
    from app.services.history_service import InvestigationHistoryService

    owner = _visible_owner(principal)
    job = get_job_store().get(investigation_id)

    if job is not None and (owner is None or not job.owner or job.owner == owner):
        result = job.result or {}
        return _diagnosis_view(investigation_id, str(job.status), job.error, result)

    report = InvestigationHistoryService().read_report(investigation_id, owner=owner)
    if report is None:
        # Absence and denial answer alike, as everywhere else: knowing an id
        # exists is not access.
        return {"error": "No such investigation."}
    return _diagnosis_view(investigation_id, "succeeded", "", report)


def _diagnosis_view(
    investigation_id: str, status: str, error: str, result: dict[str, Any]
) -> dict[str, Any]:
    investigation = result.get("investigation") or {}
    diagnosis = result.get("diagnosis") or {}
    return {
        "investigation_id": investigation_id,
        "status": status,
        "error": error or "",
        "cluster": investigation.get("context", ""),
        "severity": (investigation.get("severity") or {}).get("severity", ""),
        "health": (investigation.get("health") or {}).get("status", ""),
        "root_cause": diagnosis.get("root_cause", ""),
        "explanation": diagnosis.get("explanation", ""),
        "fix": diagnosis.get("fix", ""),
        # Deterministic and policy-checked, never model-authored — see
        # `app/ai`. Passing them on is safe in the way returning the model's
        # own suggested commands would not be.
        "commands": diagnosis.get("kubectl_commands", []),
        "confidence": diagnosis.get("confidence"),
        "ai_generated": bool(diagnosis.get("ai_generated")),
        "evidence_coverage": investigation.get("evidence_coverage", {}),
    }


async def list_investigations(principal, **_: Any) -> dict[str, Any]:
    """Recent investigations this caller may see."""
    from app.api.investigate import _visible_owner
    from app.services.history_service import InvestigationHistoryService

    items = InvestigationHistoryService().list_history(owner=_visible_owner(principal))
    return {
        "investigations": [
            {
                "investigation_id": item.get("id", ""),
                "cluster": item.get("context", ""),
                "created_at": item.get("timestamp", ""),
                "severity": item.get("severity", ""),
                "expired": bool(item.get("expired")),
            }
            for item in items
        ]
    }


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            name="list_clusters",
            description="List the Kubernetes clusters this platform can investigate.",
            permission=Permission.CLUSTER_READ,
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_clusters,
        ),
        Tool(
            name="start_investigation",
            description=(
                "Start an investigation of a cluster and return its id immediately. "
                "Reads the cluster as the calling identity; does not wait for the result."
            ),
            permission=Permission.INVESTIGATION_RUN,
            schema={
                "type": "object",
                "properties": {
                    "cluster": {"type": "string", "description": "Cluster to investigate."},
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace to scope the investigation to.",
                    },
                },
                "additionalProperties": False,
            },
            handler=start_investigation,
        ),
        Tool(
            name="get_investigation",
            description="Status and, once finished, the diagnosis for an investigation id.",
            permission=Permission.INVESTIGATION_READ,
            schema={
                "type": "object",
                "properties": {"investigation_id": {"type": "string"}},
                "required": ["investigation_id"],
                "additionalProperties": False,
            },
            handler=get_investigation,
        ),
        Tool(
            name="list_investigations",
            description="Recent investigations visible to the calling identity.",
            permission=Permission.INVESTIGATION_READ,
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_investigations,
        ),
    )
}


def get_tool(name: str) -> Tool | None:
    """The named tool, or `None` — which the caller must treat as refusal.

    Spelled as an absent value rather than an exception so the call site has to
    decide, and so the default when a name is not recognised is "cannot be
    called" rather than "call something else".
    """
    return TOOLS.get(name)
