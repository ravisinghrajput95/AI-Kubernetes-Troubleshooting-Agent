"""The evidence wire contract round-trips losslessly.

M2 delivers a schema that nothing transports yet, so the only thing that can go
wrong is a silent mapping bug — and it would surface much later, as a fleet
diagnosis that cannot be reproduced from its own evidence. These tests are
therefore the whole of M2's guarantee, and they are written to fail on a lost
field rather than to demonstrate the happy path.

The enumerated cases below are the ones that are actually hard: `None` versus
empty string, absent payload versus empty payload, non-ASCII, deep nesting, the
non-usable statuses that carry a reason instead of data. The fuzz test then
covers combinations nobody thought to enumerate.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.evidence.models import (
    Evidence,
    EvidenceKind,
    EvidenceSource,
    EvidenceStatus,
    ResourceRef,
)
from app.wire.codec import (
    WireDecodeError,
    WireEncodeError,
    decode_evidence,
    encode_evidence,
    encode_resource_ref,
)
from app.wire.gen.agent.v1 import collection_pb2, evidence_pb2

AT = datetime(2026, 7, 26, 12, 0, 0, 123456, tzinfo=UTC)


def evidence(**overrides) -> Evidence:
    base = {
        "id": "k8s.pods:pod/prod/web-0",
        "kind": EvidenceKind.PODS,
        "source": EvidenceSource.KUBECTL,
        "status": EvidenceStatus.OK,
        "target": ResourceRef(kind="Pod", name="web-0", namespace="prod"),
        "data": {"problematic_pods": [{"name": "web-0", "restarts": 4}]},
        "detail": "",
        "command": "kubectl get pods -n prod -o json",
        "collector_id": "k8s.pods",
        "duration_ms": 42,
        "redacted": True,
        "collected_at": AT,
    }
    return Evidence(**{**base, **overrides})


def roundtrip(record: Evidence) -> Evidence:
    """Through the encoder, the actual serialised bytes, and back.

    Serialising is not incidental — a field that encodes but does not survive
    `SerializeToString` is exactly the bug this must catch.
    """
    wire = encode_evidence(record).SerializeToString()
    message = evidence_pb2.EvidenceRecord()
    message.ParseFromString(wire)
    return decode_evidence(message)


CASES = {
    "healthy pod evidence": evidence(),
    "cluster-scoped target has no namespace": evidence(
        target=ResourceRef(kind="Cluster", name="prod-eu-1")
    ),
    "namespace is an empty string, not absent": evidence(
        target=ResourceRef(kind="Pod", name="web-0", namespace="")
    ),
    "target carries a uid": evidence(
        target=ResourceRef(kind="Pod", name="web-0", namespace="prod", uid="abc-123")
    ),
    "no payload at all": evidence(data=None),
    "payload is an empty dict": evidence(data={}),
    "payload is an empty list": evidence(data=[]),
    "payload is a bare scalar": evidence(data=0),
    "payload is false": evidence(data=False),
    "payload is deeply nested": evidence(
        data={"a": [{"b": {"c": [1, 2, {"d": None}]}}]},
    ),
    "payload contains non-ascii": evidence(data={"detail": "réplica 已停止 🛑"}),
    "payload contains a float": evidence(data={"memory_utilisation_percent": 90.5}),
    "no command was run": evidence(command=None),
    "command is an empty string": evidence(command=""),
    # The non-usable statuses are the ones that make a gap citable; losing the
    # reason would turn "we could not look" into "we looked and saw nothing".
    "forbidden carries its reason": evidence(
        status=EvidenceStatus.FORBIDDEN,
        data=None,
        detail="pods is forbidden: User cannot list resource",
    ),
    "not applicable names the missing backend": evidence(
        status=EvidenceStatus.NOT_APPLICABLE,
        source=EvidenceSource.PROMETHEUS,
        data=None,
        detail="Set PROMETHEUS_URL to enable metric collection.",
    ),
    "empty is usable and distinct from unavailable": evidence(
        status=EvidenceStatus.EMPTY, data={"items": []}
    ),
    "timeout": evidence(status=EvidenceStatus.TIMEOUT, data=None),
    "failed": evidence(status=EvidenceStatus.FAILED, data=None),
    "unavailable": evidence(status=EvidenceStatus.UNAVAILABLE, data=None),
    "loki source": evidence(source=EvidenceSource.LOKI, kind="loki.pod.logs"),
    "derived source": evidence(source=EvidenceSource.DERIVED),
    "unredacted": evidence(redacted=False),
    "zero duration": evidence(duration_ms=0),
    "id carries a discriminator": evidence(id="k8s.pods.logs:pod/prod/web-0#web"),
    "microsecond precision": evidence(
        collected_at=datetime(2026, 1, 1, 0, 0, 0, 999999, tzinfo=UTC)
    ),
}


class TestRoundTrip:
    @pytest.mark.parametrize("case", CASES.values(), ids=list(CASES))
    def test_every_field_survives(self, case):
        assert roundtrip(case) == case

    def test_the_comparison_would_notice_a_lost_field(self):
        """Guards the guard: `Evidence` equality must compare payloads."""
        assert evidence(data={"a": 1}) != evidence(data={"a": 2})
        assert evidence(command=None) != evidence(command="")
        assert evidence(target=ResourceRef(kind="Pod", name="web-0")) != evidence(
            target=ResourceRef(kind="Pod", name="web-0", namespace="")
        )


class TestDistinctionsThatMustSurvive:
    def test_no_payload_is_empty_bytes_not_the_string_null(self):
        assert encode_evidence(evidence(data=None)).payload == b""

    def test_a_null_payload_from_another_implementation_decodes_to_none(self):
        """Python cannot tell absent from null; a Go agent can send either.

        Both must mean the same thing on arrival, or the same fact collected by
        two agent implementations would compare unequal.
        """
        message = encode_evidence(evidence(data=None))
        message.payload = b"null"

        assert decode_evidence(message).data is None

    def test_absent_namespace_and_empty_namespace_are_different_on_the_wire(self):
        cluster_scoped = encode_resource_ref(ResourceRef(kind="Cluster", name="c"))
        empty_namespace = encode_resource_ref(ResourceRef(kind="Pod", name="p", namespace=""))

        assert not cluster_scoped.HasField("namespace")
        assert empty_namespace.HasField("namespace")

    def test_usability_is_preserved_for_every_status(self):
        for status in EvidenceStatus:
            decoded = roundtrip(evidence(status=status, data=None))
            assert decoded.usable is status.usable

    def test_the_payload_encoding_is_canonical(self):
        """Key order must not change the bytes, or identical facts would differ."""
        first = encode_evidence(evidence(data={"b": 1, "a": 2}))
        second = encode_evidence(evidence(data={"a": 2, "b": 1}))

        assert first.payload == second.payload


class TestDecodeRejection:
    """A decode failure must not become a plausible-looking record."""

    def test_a_record_without_an_id_is_rejected(self):
        message = encode_evidence(evidence())
        message.ClearField("id")

        with pytest.raises(WireDecodeError, match="cited"):
            decode_evidence(message)

    def test_an_unknown_status_is_rejected_not_defaulted(self):
        message = encode_evidence(evidence())
        message.status = evidence_pb2.EVIDENCE_STATUS_UNSPECIFIED

        with pytest.raises(WireDecodeError, match="status"):
            decode_evidence(message)

    def test_an_unknown_source_is_rejected_not_defaulted(self):
        message = encode_evidence(evidence())
        message.source = evidence_pb2.EVIDENCE_SOURCE_UNSPECIFIED

        with pytest.raises(WireDecodeError, match="source"):
            decode_evidence(message)

    def test_an_unserialisable_payload_is_rejected_not_stringified(self):
        """Coercion would round-trip a datetime into a string and lose the type."""
        with pytest.raises(WireEncodeError, match="JSON-encodable"):
            encode_evidence(evidence(data={"at": datetime.now(UTC)}))

    def test_a_corrupt_payload_is_rejected(self):
        message = encode_evidence(evidence())
        message.payload = b"\xff\xfe not json"

        with pytest.raises(WireDecodeError, match="JSON"):
            decode_evidence(message)


class TestFuzz:
    """Combinations nobody enumerated. Seeded, so a failure is reproducible."""

    def _random_payload(self, rng: random.Random, depth: int = 0):
        choices = ["scalar", "string", "none", "bool", "float"]
        if depth < 3:
            choices += ["dict", "list"]
        match rng.choice(choices):
            case "dict":
                return {
                    rng.choice(["a", "b", "réplica", "", "k8s.io/name"]): self._random_payload(
                        rng, depth + 1
                    )
                    for _ in range(rng.randint(0, 3))
                }
            case "list":
                return [self._random_payload(rng, depth + 1) for _ in range(rng.randint(0, 3))]
            case "string":
                return rng.choice(["", "ok", "已停止", "line\nbreak", '"quoted"', "\\escaped"])
            case "none":
                return None
            case "bool":
                return rng.choice([True, False])
            case "float":
                return rng.choice([0.0, -1.5, 1e12, 90.5])
            case _:
                return rng.randint(-(2**40), 2**40)

    def _random_evidence(self, rng: random.Random) -> Evidence:
        return evidence(
            id=f"{rng.choice(['k8s.pods', 'loki.pod.logs'])}:pod/prod/web-{rng.randint(0, 99)}",
            kind=rng.choice([EvidenceKind.PODS, "custom.kind", ""]),
            source=rng.choice(list(EvidenceSource)),
            status=rng.choice(list(EvidenceStatus)),
            target=ResourceRef(
                kind=rng.choice(["Pod", "Cluster", "Node"]),
                name=rng.choice(["web-0", "", "a" * 200]),
                namespace=rng.choice([None, "", "prod", "kube-system"]),
                uid=rng.choice([None, "", "abc-123"]),
            ),
            data=self._random_payload(rng),
            detail=rng.choice(["", "boom", "réplica 已停止"]),
            command=rng.choice([None, "", "kubectl get pods"]),
            collector_id=rng.choice(["", "k8s.pods"]),
            duration_ms=rng.randint(0, 2**31),
            redacted=rng.choice([True, False]),
            collected_at=AT + timedelta(microseconds=rng.randint(0, 10**9)),
        )

    @pytest.mark.parametrize("seed", range(20))
    def test_random_evidence_round_trips(self, seed):
        rng = random.Random(seed)
        for _ in range(50):
            record = self._random_evidence(rng)
            assert roundtrip(record) == record


class TestRequestsCannotCarryCommands:
    """The schema-level counterpart to `ResourceRequest`'s closed verb set.

    A platform that cannot express a command cannot instruct an agent to run
    one, which is what keeps the read-only guarantee true even if the platform
    is compromised. This test fails if a future field reintroduces the escape.
    """

    def test_evidence_spec_names_a_kind_and_nothing_executable(self):
        fields = {field.name for field in collection_pb2.EvidenceSpec.DESCRIPTOR.fields}

        assert fields == {"kind", "target", "parameters"}

    def test_a_collection_request_exposes_no_command_field(self):
        for descriptor in (
            collection_pb2.CollectionRequest.DESCRIPTOR,
            collection_pb2.EvidenceSpec.DESCRIPTOR,
            collection_pb2.Budget.DESCRIPTOR,
            collection_pb2.Impersonation.DESCRIPTOR,
        ):
            names = {field.name for field in descriptor.fields}
            assert not {"command", "args", "argv", "script", "exec"} & names, descriptor.name

    def test_the_downward_direction_carries_no_free_text_that_is_run(self):
        """Parameters are values a named collector interprets, never a command."""
        parameters = collection_pb2.EvidenceSpec.DESCRIPTOR.fields_by_name["parameters"]

        assert parameters.message_type.name == "ParametersEntry"
        assert {f.name for f in parameters.message_type.fields} == {"key", "value"}
