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
    # Singular *and* plural, because the key is whatever a collector happened
    # to type. `pod`/`pods` were both here from the start; `configmap` and
    # `ingresses` were not, and each one missing meant that read failed on
    # every agent-reached cluster and nowhere else. `tests/test_provider_parity.py`
    # derives the keys from the collectors so the next one cannot be forgotten.
    (ReadVerb.GET, "configmap"): "k8s.configmaps",
    (ReadVerb.GET, "serviceaccounts"): "k8s.serviceaccounts",
    (ReadVerb.GET, "serviceaccount"): "k8s.serviceaccounts",
    (ReadVerb.GET, "resourcequotas"): "k8s.resourcequotas",
    (ReadVerb.GET, "limitranges"): "k8s.limitranges",
    (ReadVerb.GET, "storageclasses"): "k8s.storageclasses",
    (ReadVerb.GET, "volumeattachments"): "k8s.volumeattachments",
    (ReadVerb.GET, "endpointslices"): "k8s.endpointslices",
    (ReadVerb.GET, "ingress"): "k8s.ingress",
    (ReadVerb.GET, "ingresses"): "k8s.ingress",
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


def _slot(kind: str, target: evidence_pb2.ResourceRef) -> tuple[str, str, str | None]:
    """The identity of one read: its kind and exactly what it named.

    `None` is not `""` here for the same reason it is not in the codec — a
    cluster-scoped read has no namespace, which is a different request from one
    naming a namespace called `""`. proto3 field presence carries it, so this
    reads presence rather than truthiness.
    """
    if target is None:
        return (kind, "", None)
    namespace = target.namespace if target.HasField("namespace") else None
    return (kind, target.name, namespace)


def _describe(target: evidence_pb2.ResourceRef) -> str:
    if target is None or not target.name:
        return "the requested scope"
    if target.HasField("namespace") and target.namespace:
        return f"{target.namespace}/{target.name}"
    return target.name


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

        specs: dict[int, collection_pb2.EvidenceSpec] = {}
        unsupported: dict[int, str] = {}
        for position, request in enumerate(requests):
            try:
                specs[position] = spec_for(request)
            except ProviderUnsupported as exc:
                unsupported[position] = str(exc)

        pending = await self._session.collect(
            list(specs.values()),
            investigation_id=self._investigation_id,
            actor=self._actor,
        )

        # Records are matched back to the request that asked for them by **kind
        # and target**, not by kind alone.
        #
        # A wave commonly contains several reads of one kind that differ only
        # by target — `LogsCollector` issues one `k8s.logs` per problematic pod
        # — and nothing requires an agent to answer them in the order they were
        # asked. Matching on kind and taking the next record filed pod A's logs
        # under pod B's name: measured at 5.5% of pod-log entries over an hour
        # against a real agent, counting only the mis-pairings detectable
        # because the message named a different pod than the entry it sat on.
        # The successful ones are the same defect and leave no trace at all —
        # a diagnosis quoting the wrong container's output, with a citation.
        #
        # The information to do this right was already on the wire: the agent
        # echoes `spec.target` onto every record it returns, including refusals.
        # This is what the comment here always claimed — anything unmatched is a
        # gap, never a guess — finally being true of the code.
        by_slot: dict[tuple[str, str, str | None], list[evidence_pb2.EvidenceRecord]] = {}
        for record in pending.records:
            by_slot.setdefault(_slot(record.kind, record.target), []).append(record)

        results: list[ProviderResult] = []
        for position in range(len(requests)):
            if position in unsupported:
                results.append(ProviderResult(success=False, error=unsupported[position]))
                continue

            spec = specs[position]
            records = by_slot.get(_slot(spec.kind, spec.target), [])
            if not records:
                results.append(
                    ProviderResult(
                        success=False,
                        error=(
                            pending.detail
                            or f"The agent returned no {spec.kind} evidence for "
                            f"{_describe(spec.target)}."
                        ),
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

    # The same decision the kubeconfig path makes, from the same function.
    # Sending `principal.subject` unconditionally asked the cluster to read as a
    # user named `anonymous` on an unauthenticated deployment — inert only while
    # the agent discarded the field, and refused by every real cluster the
    # moment it stopped.
    from app.auth.impersonation import identity_for

    identity = identity_for(principal)
    subject, groups = identity if identity else ("", ())
    return RemoteAgentProvider(
        session,
        investigation_id=investigation_id,
        principal_subject=subject,
        principal_groups=groups,
    )
