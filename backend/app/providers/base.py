"""How the investigation engine reaches a cluster.

The engine expresses **what** evidence it needs. It never expresses **how** to
collect it, and in particular it never constructs a command. That distinction is
what lets the same engine run against a local kubeconfig today and a remote
cluster agent later, and it is also a security property: a request that cannot
carry a command cannot instruct a remote agent to run one.

See docs/ENTERPRISE_ARCHITECTURE.md — ADR-003.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ReadVerb(StrEnum):
    """Read operations a provider may be asked to perform.

    Deliberately a closed set. A provider cannot be asked for an arbitrary
    operation, so no request — however constructed — can mutate a cluster.
    """

    GET = "get"
    DESCRIBE = "describe"
    LOGS = "logs"
    TOP = "top"
    CONFIG = "config"


class OutputFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """A declarative description of evidence to fetch.

    Every field is data. There is no field in which a command, a shell
    fragment, or a flag string can be smuggled — a provider builds whatever
    invocation its transport needs from these values alone.
    """

    verb: ReadVerb
    resource: str = ""
    name: str | None = None
    namespace: str | None = None
    all_namespaces: bool = False
    output: OutputFormat = OutputFormat.JSON
    # Label and field selectors, expressed as data rather than flag strings.
    label_selector: str | None = None
    field_selector: str | None = None
    # Verb-specific options: tail, previous, all_containers, no_headers, subcommand.
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def is_list(self) -> bool:
        """True when the request can return an unbounded number of objects."""
        return self.verb is ReadVerb.GET and self.name is None

    def describe(self) -> str:
        """Short human-readable form, for logs and progress messages."""
        target = self.name or ("all" if self.all_namespaces else self.namespace or "cluster")
        return f"{self.verb} {self.resource or ''} {target}".strip()


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """The outcome of one request, independent of how it was obtained."""

    success: bool
    data: dict[str, Any] | list[Any] | None = None
    text: str = ""
    error: str = ""
    # The equivalent command, retained so a human can reproduce the read.
    # It is a record of what happened, never an instruction to execute.
    equivalent_command: str = ""
    truncated: bool = False
    total_items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.equivalent_command,
            "success": self.success,
            "stdout": self.text,
            "stderr": self.error,
            "data": self.data,
            "truncated": self.truncated,
            "total_items": self.total_items,
        }


class ProviderUnsupported(RuntimeError):
    """Raised when a provider cannot serve a request.

    Notably raised by remote providers for the legacy raw-command escape hatch,
    so a collector that has not migrated to `ResourceRequest` fails loudly
    against a remote cluster instead of silently working only in development.
    """


@runtime_checkable
class ClusterProvider(Protocol):
    """The engine's only route to a cluster."""

    @property
    def cluster_id(self) -> str: ...

    @property
    def executed_commands(self) -> list[str]:
        """Audit trail of reads performed, in human-reproducible form."""
        ...

    @property
    def truncations(self) -> list[dict[str, Any]]:
        """Reads that returned more objects than were retained."""
        ...

    async def fetch(self, request: ResourceRequest) -> ProviderResult: ...

    async def fetch_many(self, requests: Sequence[ResourceRequest]) -> Sequence[ProviderResult]: ...
