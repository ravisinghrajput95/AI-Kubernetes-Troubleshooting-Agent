"""`Evidence` ↔ protobuf, losslessly in both directions.

The evidence spine is the platform's central abstraction: every conclusion cites
an evidence id, and a non-usable status is itself citable data. If a value can be
altered by a round trip through the wire, a fleet diagnosis stops being
reproducible from the same evidence — so the mapping is total, and
`tests/test_wire_contract.py` fuzzes it rather than spot-checking it.

Three places where lossless is harder than it looks, and what is done about each:

- **`data` is `Any`.** Encoded as JSON in an opaque `bytes` field, so the schema
  never has to know a collector's payload shape. Absent data is empty bytes,
  which Python cannot distinguish from JSON `null` anyway — but a Go agent can
  send either, so the decoder accepts both and canonicalises to `None`.
  Otherwise the same fact collected by two agent implementations would compare
  unequal.
- **Optional strings.** `namespace`, `uid` and `command` are `str | None`, and
  `None` is not the empty string — a cluster-scoped object has no namespace,
  which is not the same as a namespace named "". proto3 field presence carries
  the distinction.
- **Timestamps.** `Timestamp` is nanosecond-precision, `datetime` is
  microsecond, so the value survives; the timezone does not travel and is
  restored as UTC. A naive datetime is *treated* as UTC rather than rejected,
  because rejecting it would turn a cosmetic problem into a lost record.
"""

import json
from datetime import UTC, datetime
from typing import Any

from app.evidence.models import Evidence, EvidenceSource, EvidenceStatus, ResourceRef
from app.wire.gen.agent.v1 import evidence_pb2

_STATUS_TO_WIRE = {
    EvidenceStatus.OK: evidence_pb2.EVIDENCE_STATUS_OK,
    EvidenceStatus.EMPTY: evidence_pb2.EVIDENCE_STATUS_EMPTY,
    EvidenceStatus.UNAVAILABLE: evidence_pb2.EVIDENCE_STATUS_UNAVAILABLE,
    EvidenceStatus.FORBIDDEN: evidence_pb2.EVIDENCE_STATUS_FORBIDDEN,
    EvidenceStatus.TIMEOUT: evidence_pb2.EVIDENCE_STATUS_TIMEOUT,
    EvidenceStatus.NOT_APPLICABLE: evidence_pb2.EVIDENCE_STATUS_NOT_APPLICABLE,
    EvidenceStatus.FAILED: evidence_pb2.EVIDENCE_STATUS_FAILED,
}
_STATUS_FROM_WIRE = {wire: local for local, wire in _STATUS_TO_WIRE.items()}

_SOURCE_TO_WIRE = {
    EvidenceSource.KUBECTL: evidence_pb2.EVIDENCE_SOURCE_KUBECTL,
    EvidenceSource.PROMETHEUS: evidence_pb2.EVIDENCE_SOURCE_PROMETHEUS,
    EvidenceSource.LOKI: evidence_pb2.EVIDENCE_SOURCE_LOKI,
    EvidenceSource.DERIVED: evidence_pb2.EVIDENCE_SOURCE_DERIVED,
}
_SOURCE_FROM_WIRE = {wire: local for local, wire in _SOURCE_TO_WIRE.items()}


class WireError(ValueError):
    """Base for both directions of the wire mapping."""


class WireDecodeError(WireError):
    """A message could not be decoded into evidence.

    Raised rather than returning a degraded record: a decode failure is a
    protocol bug or a hostile peer, and either way the platform must not invent
    evidence to paper over it.
    """


class WireEncodeError(WireError):
    """Evidence could not be represented on the wire without losing something."""


def encode_resource_ref(target: ResourceRef) -> evidence_pb2.ResourceRef:
    message = evidence_pb2.ResourceRef(kind=target.kind, name=target.name)
    if target.namespace is not None:
        message.namespace = target.namespace
    if target.uid is not None:
        message.uid = target.uid
    return message


def decode_resource_ref(message: evidence_pb2.ResourceRef) -> ResourceRef:
    return ResourceRef(
        kind=message.kind,
        name=message.name,
        namespace=message.namespace if message.HasField("namespace") else None,
        uid=message.uid if message.HasField("uid") else None,
    )


def encode_evidence(evidence: Evidence) -> evidence_pb2.EvidenceRecord:
    """Evidence → wire. Every field travels; none are summarised away."""
    message = evidence_pb2.EvidenceRecord(
        id=evidence.id,
        kind=evidence.kind,
        source=_SOURCE_TO_WIRE[EvidenceSource(evidence.source)],
        status=_STATUS_TO_WIRE[EvidenceStatus(evidence.status)],
        target=encode_resource_ref(evidence.target),
        payload=_encode_payload(evidence.data),
        detail=evidence.detail,
        duration_ms=evidence.duration_ms,
        collector_id=evidence.collector_id,
        redacted=evidence.redacted,
    )
    if evidence.command is not None:
        message.equivalent_command = evidence.command
    message.collected_at.FromDatetime(_as_utc(evidence.collected_at))
    return message


def decode_evidence(message: evidence_pb2.EvidenceRecord) -> Evidence:
    """Wire → evidence. Rejects anything it cannot represent faithfully."""
    if message.status not in _STATUS_FROM_WIRE:
        raise WireDecodeError(f"Unknown evidence status on the wire: {message.status}")
    if message.source not in _SOURCE_FROM_WIRE:
        raise WireDecodeError(f"Unknown evidence source on the wire: {message.source}")
    if not message.id:
        raise WireDecodeError("Evidence record has no id; it could not be cited")

    return Evidence(
        id=message.id,
        kind=message.kind,
        source=_SOURCE_FROM_WIRE[message.source],
        status=_STATUS_FROM_WIRE[message.status],
        target=decode_resource_ref(message.target),
        data=_decode_payload(message.payload),
        detail=message.detail,
        command=(message.equivalent_command if message.HasField("equivalent_command") else None),
        collector_id=message.collector_id,
        duration_ms=message.duration_ms,
        redacted=message.redacted,
        collected_at=message.collected_at.ToDatetime(tzinfo=UTC),
    )


def _encode_payload(data: Any) -> bytes:
    if data is None:
        return b""
    try:
        # sort_keys makes the encoding canonical, so an unchanged payload has an
        # unchanged wire representation and is comparable across collections.
        #
        # Deliberately no `default=` fallback. Coercing an unserialisable value
        # to its repr would round-trip a datetime into a string and lose the
        # type silently. `history_service` already requires payloads to be
        # strictly JSON-serialisable, so failing here is consistent — and a loud
        # failure is the only kind that gets fixed.
        return json.dumps(data, sort_keys=True).encode()
    except (TypeError, ValueError) as error:
        raise WireEncodeError(f"Evidence payload is not JSON-encodable: {error}") from error


def _decode_payload(payload: bytes) -> Any:
    if not payload:
        return None
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WireDecodeError(f"Evidence payload is not valid JSON: {error}") from error


def _as_utc(moment: datetime) -> datetime:
    # `FromDatetime` treats a naive value as UTC already; being explicit means a
    # naive datetime and its aware equivalent encode identically.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
