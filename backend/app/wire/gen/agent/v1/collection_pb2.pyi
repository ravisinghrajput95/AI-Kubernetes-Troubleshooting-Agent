from app.wire.gen.agent.v1 import evidence_pb2 as _evidence_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Impersonation(_message.Message):
    __slots__ = ("username", "groups")
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    GROUPS_FIELD_NUMBER: _ClassVar[int]
    username: str
    groups: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, username: _Optional[str] = ..., groups: _Optional[_Iterable[str]] = ...) -> None: ...

class Budget(_message.Message):
    __slots__ = ("deadline_ms", "max_items", "max_bytes")
    DEADLINE_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_ITEMS_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    deadline_ms: int
    max_items: int
    max_bytes: int
    def __init__(self, deadline_ms: _Optional[int] = ..., max_items: _Optional[int] = ..., max_bytes: _Optional[int] = ...) -> None: ...

class EvidenceSpec(_message.Message):
    __slots__ = ("kind", "target", "parameters")
    class ParametersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    KIND_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    kind: str
    target: _evidence_pb2.ResourceRef
    parameters: _containers.ScalarMap[str, str]
    def __init__(self, kind: _Optional[str] = ..., target: _Optional[_Union[_evidence_pb2.ResourceRef, _Mapping]] = ..., parameters: _Optional[_Mapping[str, str]] = ...) -> None: ...

class CollectionRequest(_message.Message):
    __slots__ = ("investigation_id", "request_id", "specs", "actor", "budget")
    INVESTIGATION_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SPECS_FIELD_NUMBER: _ClassVar[int]
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    BUDGET_FIELD_NUMBER: _ClassVar[int]
    investigation_id: str
    request_id: str
    specs: _containers.RepeatedCompositeFieldContainer[EvidenceSpec]
    actor: Impersonation
    budget: Budget
    def __init__(self, investigation_id: _Optional[str] = ..., request_id: _Optional[str] = ..., specs: _Optional[_Iterable[_Union[EvidenceSpec, _Mapping]]] = ..., actor: _Optional[_Union[Impersonation, _Mapping]] = ..., budget: _Optional[_Union[Budget, _Mapping]] = ...) -> None: ...

class CancelRequest(_message.Message):
    __slots__ = ("investigation_id", "reason")
    INVESTIGATION_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    investigation_id: str
    reason: str
    def __init__(self, investigation_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class CollectionDone(_message.Message):
    __slots__ = ("investigation_id", "request_id", "records_emitted", "specs_requested", "detail")
    INVESTIGATION_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    RECORDS_EMITTED_FIELD_NUMBER: _ClassVar[int]
    SPECS_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    investigation_id: str
    request_id: str
    records_emitted: int
    specs_requested: int
    detail: str
    def __init__(self, investigation_id: _Optional[str] = ..., request_id: _Optional[str] = ..., records_emitted: _Optional[int] = ..., specs_requested: _Optional[int] = ..., detail: _Optional[str] = ...) -> None: ...
