import datetime

from app.wire.gen.agent.v1 import collection_pb2 as _collection_pb2
from app.wire.gen.agent.v1 import evidence_pb2 as _evidence_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PlatformMessage(_message.Message):
    __slots__ = ("collect", "cancel", "capability", "config", "heartbeat")
    COLLECT_FIELD_NUMBER: _ClassVar[int]
    CANCEL_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    collect: _collection_pb2.CollectionRequest
    cancel: _collection_pb2.CancelRequest
    capability: CapabilityQuery
    config: ConfigUpdate
    heartbeat: Heartbeat
    def __init__(self, collect: _Optional[_Union[_collection_pb2.CollectionRequest, _Mapping]] = ..., cancel: _Optional[_Union[_collection_pb2.CancelRequest, _Mapping]] = ..., capability: _Optional[_Union[CapabilityQuery, _Mapping]] = ..., config: _Optional[_Union[ConfigUpdate, _Mapping]] = ..., heartbeat: _Optional[_Union[Heartbeat, _Mapping]] = ...) -> None: ...

class AgentMessage(_message.Message):
    __slots__ = ("hello", "evidence", "done", "health", "event")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    HEALTH_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    hello: AgentHello
    evidence: EvidenceEnvelope
    done: _collection_pb2.CollectionDone
    health: AgentHealth
    event: ClusterEvent
    def __init__(self, hello: _Optional[_Union[AgentHello, _Mapping]] = ..., evidence: _Optional[_Union[EvidenceEnvelope, _Mapping]] = ..., done: _Optional[_Union[_collection_pb2.CollectionDone, _Mapping]] = ..., health: _Optional[_Union[AgentHealth, _Mapping]] = ..., event: _Optional[_Union[ClusterEvent, _Mapping]] = ...) -> None: ...

class EvidenceEnvelope(_message.Message):
    __slots__ = ("investigation_id", "request_id", "record")
    INVESTIGATION_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    RECORD_FIELD_NUMBER: _ClassVar[int]
    investigation_id: str
    request_id: str
    record: _evidence_pb2.EvidenceRecord
    def __init__(self, investigation_id: _Optional[str] = ..., request_id: _Optional[str] = ..., record: _Optional[_Union[_evidence_pb2.EvidenceRecord, _Mapping]] = ...) -> None: ...

class AgentHello(_message.Message):
    __slots__ = ("cluster_id", "agent_version", "kubernetes_version", "supported_kinds", "available_backends", "protocol_version")
    CLUSTER_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    KUBERNETES_VERSION_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_KINDS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_BACKENDS_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    cluster_id: str
    agent_version: str
    kubernetes_version: str
    supported_kinds: _containers.RepeatedScalarFieldContainer[str]
    available_backends: _containers.RepeatedScalarFieldContainer[str]
    protocol_version: int
    def __init__(self, cluster_id: _Optional[str] = ..., agent_version: _Optional[str] = ..., kubernetes_version: _Optional[str] = ..., supported_kinds: _Optional[_Iterable[str]] = ..., available_backends: _Optional[_Iterable[str]] = ..., protocol_version: _Optional[int] = ...) -> None: ...

class CapabilityQuery(_message.Message):
    __slots__ = ("request_id",)
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    def __init__(self, request_id: _Optional[str] = ...) -> None: ...

class ConfigUpdate(_message.Message):
    __slots__ = ("default_budget", "log_level", "event_sample_rate")
    DEFAULT_BUDGET_FIELD_NUMBER: _ClassVar[int]
    LOG_LEVEL_FIELD_NUMBER: _ClassVar[int]
    EVENT_SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    default_budget: _collection_pb2.Budget
    log_level: str
    event_sample_rate: int
    def __init__(self, default_budget: _Optional[_Union[_collection_pb2.Budget, _Mapping]] = ..., log_level: _Optional[str] = ..., event_sample_rate: _Optional[int] = ...) -> None: ...

class Heartbeat(_message.Message):
    __slots__ = ("sent_at",)
    SENT_AT_FIELD_NUMBER: _ClassVar[int]
    sent_at: _timestamp_pb2.Timestamp
    def __init__(self, sent_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class AgentHealth(_message.Message):
    __slots__ = ("reported_at", "active_collections", "queued_collections", "degradation")
    REPORTED_AT_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_COLLECTIONS_FIELD_NUMBER: _ClassVar[int]
    QUEUED_COLLECTIONS_FIELD_NUMBER: _ClassVar[int]
    DEGRADATION_FIELD_NUMBER: _ClassVar[int]
    reported_at: _timestamp_pb2.Timestamp
    active_collections: int
    queued_collections: int
    degradation: str
    def __init__(self, reported_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., active_collections: _Optional[int] = ..., queued_collections: _Optional[int] = ..., degradation: _Optional[str] = ...) -> None: ...

class ClusterEvent(_message.Message):
    __slots__ = ("cluster_id", "target", "reason", "message", "observed_at")
    CLUSTER_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    cluster_id: str
    target: _evidence_pb2.ResourceRef
    reason: str
    message: str
    observed_at: _timestamp_pb2.Timestamp
    def __init__(self, cluster_id: _Optional[str] = ..., target: _Optional[_Union[_evidence_pb2.ResourceRef, _Mapping]] = ..., reason: _Optional[str] = ..., message: _Optional[str] = ..., observed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class RegistrationRequest(_message.Message):
    __slots__ = ("bootstrap_token", "cluster_id", "certificate_signing_request", "agent_version")
    BOOTSTRAP_TOKEN_FIELD_NUMBER: _ClassVar[int]
    CLUSTER_ID_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_SIGNING_REQUEST_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    bootstrap_token: str
    cluster_id: str
    certificate_signing_request: bytes
    agent_version: str
    def __init__(self, bootstrap_token: _Optional[str] = ..., cluster_id: _Optional[str] = ..., certificate_signing_request: _Optional[bytes] = ..., agent_version: _Optional[str] = ...) -> None: ...

class RegistrationResponse(_message.Message):
    __slots__ = ("certificate", "ca_bundle", "expires_at", "gateway_endpoint")
    CERTIFICATE_FIELD_NUMBER: _ClassVar[int]
    CA_BUNDLE_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    certificate: bytes
    ca_bundle: bytes
    expires_at: _timestamp_pb2.Timestamp
    gateway_endpoint: str
    def __init__(self, certificate: _Optional[bytes] = ..., ca_bundle: _Optional[bytes] = ..., expires_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., gateway_endpoint: _Optional[str] = ...) -> None: ...
