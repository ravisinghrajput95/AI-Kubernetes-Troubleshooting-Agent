"""Reaching a cluster through its agent.

The engine does not know this exists. It asks a `ClusterProvider` for evidence
by describing what it wants, and this turns that description into an
`EvidenceSpec` on a stream some agent already opened. M1 predicted the swap
would be a substitution at one field rather than a refactor; this is where that
prediction is either true or it is not.

**A `ResourceRequest` becomes a `kind`, never a command.** That is the security
property the whole design rests on: the platform can name a kind of evidence
the agent already knows how to collect, and there is no field in which anything
else can be smuggled. An agent that does not recognise a kind refuses it.
"""

from collections.abc import Sequence
from typing import Any

from loguru import logger

from app.gateway.session import AgentSession
from app.providers.base import (
    OutputFormat,
    ProviderResult,
    ProviderUnsupported,
    ReadVerb,
    ResourceRequest,
)
from app.wire.gen.agent.v1 import collection_pb2, evidence_pb2

# `ResourceRequest` → the kind of evidence an agent is asked for.
#
# Deliberately a table and not a rule: a computed kind would let an unexpected
# resource name reach the agent as a novel kind, and the point of the closed set
# is that it cannot.
_KINDS: dict[tuple[ReadVerb, str], str] = {
    (ReadVerb.GET, "pods"): "k8s.pods",
    (ReadVerb.GET, "pod"): "k8s.pods",
    (ReadVerb.GET, "events"): "k8s.events",
    (ReadVerb.GET, "deployments"): "k8s.deployments",
    (ReadVerb.GET, "nodes"): "k8s.nodes",
    (ReadVerb.GET, "services"): "k8s.services",
    (ReadVerb.GET, "endpoints"): "k8s.endpoints",
    (ReadVerb.GET, "pvc"): "k8s.pvc",
    (ReadVerb.GET, "pv"): "k8s.pv",
    (ReadVerb.GET, "statefulsets"): "k8s.statefulsets",
    (ReadVerb.GET, "daemonsets"): "k8s.daemonsets",
    (ReadVerb.GET, "jobs"): "k8s.jobs",
    (ReadVerb.GET, "cronjobs"): "k8s.cronjobs",
    (ReadVerb.GET, "namespaces"): "k8s.namespaces",
    (ReadVerb.GET, "configmaps"): "k8s.configmaps",
    (ReadVerb.GET, "ingress"): "k8s.ingress",
    (ReadVerb.GET, "networkpolicies"): "k8s.networkpolicies",
    (ReadVerb.LOGS, ""): "k8s.logs",
    (ReadVerb.TOP, "nodes"): "k8s.metrics.nodes",
    (ReadVerb.TOP, "pods"): "k8s.metrics.pods",
}

_STATUS_TEXT = {
    evidence_pb2.EVIDENCE_STATUS_OK: "ok",
    evidence_pb2.EVIDENCE_STATUS_EMPTY: "empty",
    evidence_pb2.EVIDENCE_STATUS_UNAVAILABLE: "unavailable",
    evidence_pb2.EVIDENCE_STATUS_FORBIDDEN: "forbidden",
    evidence_pb2.EVIDENCE_STATUS_TIMEOUT: "timeout",
    evidence_pb2.EVIDENCE_STATUS_NOT_APPLICABLE: "not_applicable",
    evidence_pb2.EVIDENCE_STATUS_FAILED: "failed",
}

_USABLE = {evidence_pb2.EVIDENCE_STATUS_OK, evidence_pb2.EVIDENCE_STATUS_EMPTY}


def kind_for(request: ResourceRequest) -> str | None:
    """The evidence kind this request asks for, or None if there is not one."""
    if request.verb is ReadVerb.LOGS:
        return _KINDS[(ReadVerb.LOGS, "")]
    return _KINDS.get((request.verb, request.resource))


def spec_for(request: ResourceRequest) -> collection_pb2.EvidenceSpec:
    """Translate a request into a spec. Raises if there is no kind for it."""
    kind = kind_for(request)
    if kind is None:
        raise ProviderUnsupported(
            f"No evidence kind for {request.verb} {request.resource!r}; a remote "
            f"agent is only ever asked for kinds it already knows."
        )

    target = evidence_pb2.ResourceRef(
        kind=request.resource or "cluster",
        name=request.name or "",
    )
    if request.namespace:
        target.namespace = request.namespace

    # Parameters are values the named collector interprets. None of them is a
    # flag, and none of them reaches a shell on the far side.
    parameters: dict[str, str] = {}
    if request.all_namespaces:
        parameters["all_namespaces"] = "true"
    if request.label_selector:
        parameters["label_selector"] = request.label_selector
    if request.field_selector:
        parameters["field_selector"] = request.field_selector
    if request.output is OutputFormat.TEXT:
        parameters["output"] = "text"
    for key, value in request.options.items():
        parameters[str(key)] = str(value)

    return collection_pb2.EvidenceSpec(kind=kind, target=target, parameters=parameters)


class RemoteAgentProvider:
    """A `ClusterProvider` served by an agent inside the cluster."""

    def __init__(
        self,
        session: AgentSession,
        investigation_id: str = "",
        principal_subject: str = "",
        principal_groups: Sequence[str] = (),
    ) -> None:
        self._session = session
        self._investigation_id = investigation_id
        self._actor = (
            collection_pb2.Impersonation(username=principal_subject, groups=list(principal_groups))
            if principal_subject
            else None
        )
        self._executed: list[str] = []
        self._truncations: list[dict[str, Any]] = []

    @property
    def cluster_id(self) -> str:
        return self._session.cluster_id

    @property
    def executed_commands(self) -> list[str]:
        return list(self._executed)

    @property
    def truncations(self) -> list[dict[str, Any]]:
        return list(self._truncations)

    async def fetch(self, request: ResourceRequest) -> ProviderResult:
        results = await self.fetch_many([request])
        return results[0]

    async def fetch_many(self, requests: Sequence[ResourceRequest]) -> Sequence[ProviderResult]:
        """One round trip for the whole wave.

        The scheduler already batches independent collectors, so sending them
        together is what keeps a remote cluster from costing one round trip per
        read.
        """
        if not requests:
            return []

        specs: list[collection_pb2.EvidenceSpec] = []
        unsupported: dict[int, str] = {}
        for position, request in enumerate(requests):
            try:
                specs.append(spec_for(request))
            except ProviderUnsupported as exc:
                unsupported[position] = str(exc)

        pending = await self._session.collect(
            specs,
            investigation_id=self._investigation_id,
            actor=self._actor,
        )

        # Records arrive tagged with their kind; match them back to the request
        # that asked for it. Anything unmatched is a gap, never a guess.
        by_kind: dict[str, list[evidence_pb2.EvidenceRecord]] = {}
        for record in pending.records:
            by_kind.setdefault(record.kind, []).append(record)

        results: list[ProviderResult] = []
        for position, request in enumerate(requests):
            if position in unsupported:
                results.append(ProviderResult(success=False, error=unsupported[position]))
                continue

            kind = kind_for(request)
            records = by_kind.get(kind or "", [])
            if not records:
                results.append(
                    ProviderResult(
                        success=False,
                        error=pending.detail or f"The agent returned no {kind} evidence.",
                        equivalent_command="",
                    )
                )
                continue

            results.append(self._to_result(records.pop(0)))

        return results

    def _to_result(self, record: evidence_pb2.EvidenceRecord) -> ProviderResult:
        from app.wire.codec import decode_payload

        command = record.equivalent_command if record.HasField("equivalent_command") else ""
        if command:
            self._executed.append(command)

        if record.status not in _USABLE:
            return ProviderResult(
                success=False,
                error=record.detail or _STATUS_TEXT.get(record.status, "failed"),
                equivalent_command=command,
            )

        payload = decode_payload(record.payload)
        text = ""
        data: Any = payload
        if isinstance(payload, dict) and "text" in payload and len(payload) == 1:
            # A text read (logs, top) travels as a one-key object so the wire
            # format stays uniform; unwrap it back to what the engine expects.
            text = str(payload["text"])
            data = None

        return ProviderResult(
            success=True,
            data=data if isinstance(data, dict | list) else None,
            text=text,
            equivalent_command=command,
        )


def build_remote_provider(
    session: AgentSession,
    investigation_id: str = "",
    principal=None,
) -> RemoteAgentProvider:
    logger.debug("Using the remote agent for cluster {cluster}", cluster=session.cluster_id)
    return RemoteAgentProvider(
        session,
        investigation_id=investigation_id,
        principal_subject=principal.subject if principal else "",
        principal_groups=principal.groups if principal else (),
    )
