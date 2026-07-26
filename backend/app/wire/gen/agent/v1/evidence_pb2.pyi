import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EvidenceStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVIDENCE_STATUS_UNSPECIFIED: _ClassVar[EvidenceStatus]
    EVIDENCE_STATUS_OK: _ClassVar[EvidenceStatus]
    EVIDENCE_STATUS_EMPTY: _ClassVar[EvidenceStatus]
    EVIDENCE_STATUS_UNAVAILABLE: _ClassVar[EvidenceStatus]
    EVIDENCE_STATUS_FORBIDDEN: _ClassVar[EvidenceStatus]
    EVIDENCE_STATUS_TIMEOUT: _ClassVar[EvidenceStatus]
    EVIDENCE_STATUS_NOT_APPLICABLE: _ClassVar[EvidenceStatus]
    EVIDENCE_STATUS_FAILED: _ClassVar[EvidenceStatus]

class EvidenceSource(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVIDENCE_SOURCE_UNSPECIFIED: _ClassVar[EvidenceSource]
    EVIDENCE_SOURCE_KUBECTL: _ClassVar[EvidenceSource]
    EVIDENCE_SOURCE_PROMETHEUS: _ClassVar[EvidenceSource]
    EVIDENCE_SOURCE_LOKI: _ClassVar[EvidenceSource]
    EVIDENCE_SOURCE_DERIVED: _ClassVar[EvidenceSource]
EVIDENCE_STATUS_UNSPECIFIED: EvidenceStatus
EVIDENCE_STATUS_OK: EvidenceStatus
EVIDENCE_STATUS_EMPTY: EvidenceStatus
EVIDENCE_STATUS_UNAVAILABLE: EvidenceStatus
EVIDENCE_STATUS_FORBIDDEN: EvidenceStatus
EVIDENCE_STATUS_TIMEOUT: EvidenceStatus
EVIDENCE_STATUS_NOT_APPLICABLE: EvidenceStatus
EVIDENCE_STATUS_FAILED: EvidenceStatus
EVIDENCE_SOURCE_UNSPECIFIED: EvidenceSource
EVIDENCE_SOURCE_KUBECTL: EvidenceSource
EVIDENCE_SOURCE_PROMETHEUS: EvidenceSource
EVIDENCE_SOURCE_LOKI: EvidenceSource
EVIDENCE_SOURCE_DERIVED: EvidenceSource

class ResourceRef(_message.Message):
    __slots__ = ("kind", "name", "namespace", "uid")
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    kind: str
    name: str
    namespace: str
    uid: str
    def __init__(self, kind: _Optional[str] = ..., name: _Optional[str] = ..., namespace: _Optional[str] = ..., uid: _Optional[str] = ...) -> None: ...

class EvidenceRecord(_message.Message):
    __slots__ = ("id", "kind", "source", "status", "target", "payload", "equivalent_command", "detail", "duration_ms", "collected_at", "collector_id", "redacted")
    ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    EQUIVALENT_COMMAND_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    COLLECTED_AT_FIELD_NUMBER: _ClassVar[int]
    COLLECTOR_ID_FIELD_NUMBER: _ClassVar[int]
    REDACTED_FIELD_NUMBER: _ClassVar[int]
    id: str
    kind: str
    source: EvidenceSource
    status: EvidenceStatus
    target: ResourceRef
    payload: bytes
    equivalent_command: str
    detail: str
    duration_ms: int
    collected_at: _timestamp_pb2.Timestamp
    collector_id: str
    redacted: bool
    def __init__(self, id: _Optional[str] = ..., kind: _Optional[str] = ..., source: _Optional[_Union[EvidenceSource, str]] = ..., status: _Optional[_Union[EvidenceStatus, str]] = ..., target: _Optional[_Union[ResourceRef, _Mapping]] = ..., payload: _Optional[bytes] = ..., equivalent_command: _Optional[str] = ..., detail: _Optional[str] = ..., duration_ms: _Optional[int] = ..., collected_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., collector_id: _Optional[str] = ..., redacted: _Optional[bool] = ...) -> None: ...
