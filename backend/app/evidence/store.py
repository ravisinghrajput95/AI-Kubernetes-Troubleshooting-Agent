from collections.abc import Iterable, Iterator
from typing import Any

from loguru import logger

from app.evidence.models import Evidence, EvidenceStatus, ResourceRef


class EvidenceStore:
    """Addressable collection of evidence gathered during one investigation.

    Mutation happens exclusively on the asyncio event loop inside the
    scheduler, so no locking is required; collectors themselves receive the
    store read-only in practice and return evidence rather than inserting it.
    """

    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}
        self._by_kind: dict[str, list[str]] = {}

    def add(self, evidence: Evidence) -> None:
        if evidence.id in self._items:
            logger.warning(
                "Duplicate evidence id ignored: {id} (collector={collector})",
                id=evidence.id,
                collector=evidence.collector_id,
            )
            return

        self._items[evidence.id] = evidence
        self._by_kind.setdefault(evidence.kind, []).append(evidence.id)

    def extend(self, items: Iterable[Evidence]) -> None:
        for evidence in items:
            self.add(evidence)

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def by_kind(self, kind: str) -> list[Evidence]:
        return [self._items[item_id] for item_id in self._by_kind.get(kind, [])]

    def by_target(self, target: ResourceRef) -> list[Evidence]:
        return [item for item in self._items.values() if item.target.key == target.key]

    def first(self, kind: str) -> Evidence | None:
        items = self.by_kind(kind)
        return items[0] if items else None

    def has(self, kind: str) -> bool:
        """True when at least one usable piece of evidence exists for `kind`."""
        return any(item.usable for item in self.by_kind(kind))

    def data(self, kind: str, default: Any = None) -> Any:
        """Payload of the first usable evidence of `kind`, else `default`."""
        for item in self.by_kind(kind):
            if item.usable:
                return item.data
        return default

    def coverage(self) -> dict[str, Any]:
        """Per-status counts, used to score evidence completeness.

        `NOT_APPLICABLE` evidence is excluded from the completeness ratio. It
        means the evidence never applied — an optional backend that is not
        deployed, or a collector skipped for this scope — which is different
        from having tried and failed. Counting it as a gap would permanently
        cap completeness on any cluster without Prometheus, and so would lower
        confidence in diagnoses that never needed metrics.

        It still appears in `degraded`, because "the platform could have seen
        more" is worth telling an operator.
        """
        counts: dict[str, int] = {}
        for item in self._items.values():
            counts[str(item.status)] = counts.get(str(item.status), 0) + 1

        applicable = [
            item
            for item in self._items.values()
            if item.status is not EvidenceStatus.NOT_APPLICABLE
        ]
        usable = sum(1 for item in applicable if item.usable)
        degraded = [
            {"kind": item.kind, "status": str(item.status), "detail": item.detail}
            for item in self._items.values()
            if not item.usable
        ]

        return {
            "total": len(self._items),
            "applicable": len(applicable),
            "usable": usable,
            "not_applicable": len(self._items) - len(applicable),
            "completeness": round(usable / len(applicable) * 100) if applicable else 0,
            "by_status": counts,
            "degraded": degraded,
        }

    def index(self) -> list[dict[str, Any]]:
        return [item.to_index_entry() for item in self._items.values()]

    def statuses(self) -> dict[str, str]:
        return {item.kind: str(item.status) for item in self._items.values()}

    def __iter__(self) -> Iterator[Evidence]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)
