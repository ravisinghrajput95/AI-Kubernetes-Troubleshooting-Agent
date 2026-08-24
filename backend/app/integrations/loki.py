"""Loki query client.

Same contract as the Prometheus client: operational problems become a result
carrying the reason, never an exception.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings
from app.evidence.models import EvidenceStatus

MAX_LINES = 200


@dataclass(frozen=True)
class LogQueryResult:
    status: EvidenceStatus
    entries: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    query: str = ""

    @property
    def usable(self) -> bool:
        return self.status.usable


class LokiClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.loki_url).rstrip("/")
        self.timeout = timeout or settings.observability_timeout_seconds
        self.tenant_id = tenant_id if tenant_id is not None else settings.loki_tenant_id

    @property
    def headers(self) -> dict[str, str]:
        """`X-Scope-OrgID` when configured, nothing when not.

        A multi-tenant Loki rejects a query with no org id — 401, "no org id" —
        which this client correctly records as `unavailable` and an
        investigation correctly reports as a backend it could not reach. So the
        failure is legible; what was missing was any way to succeed. Absent the
        setting the header is not sent at all, because single-tenant Loki
        rejects an unexpected one.
        """
        return {"X-Scope-OrgID": self.tenant_id} if self.tenant_id else {}

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def query_range(
        self,
        logql: str,
        lookback_minutes: int | None = None,
        limit: int = MAX_LINES,
    ) -> LogQueryResult:
        if not self.configured:
            return LogQueryResult(
                EvidenceStatus.NOT_APPLICABLE,
                detail="Loki is not configured (set LOKI_URL).",
                query=logql,
            )

        end = datetime.now(UTC)
        start = end - timedelta(minutes=lookback_minutes or settings.metrics_lookback_minutes)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                response = await client.get(
                    f"{self.base_url}/loki/api/v1/query_range",
                    params={
                        "query": logql,
                        "start": int(start.timestamp() * 1e9),
                        "end": int(end.timestamp() * 1e9),
                        "limit": limit,
                        "direction": "backward",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException:
            return LogQueryResult(
                EvidenceStatus.TIMEOUT,
                detail=f"Loki did not respond within {self.timeout:.0f}s.",
                query=logql,
            )
        except httpx.HTTPError as exc:
            logger.warning("Loki query failed: {error}", error=str(exc))
            return LogQueryResult(
                EvidenceStatus.UNAVAILABLE,
                detail=f"Loki is unreachable: {exc}",
                query=logql,
            )
        except ValueError:
            return LogQueryResult(
                EvidenceStatus.FAILED,
                detail="Loki returned a response that was not valid JSON.",
                query=logql,
            )

        if payload.get("status") != "success":
            return LogQueryResult(
                EvidenceStatus.FAILED,
                detail="Loki rejected the query.",
                query=logql,
            )

        entries = self._parse(payload.get("data", {}), limit)
        return LogQueryResult(
            EvidenceStatus.OK if entries else EvidenceStatus.EMPTY,
            entries=entries,
            query=logql,
        )

    def _parse(self, data: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        for stream in data.get("result", []):
            labels = stream.get("stream", {})
            for value in stream.get("values", []):
                if len(value) < 2:
                    continue
                entries.append(
                    {
                        "timestamp": self._to_iso(value[0]),
                        "line": str(value[1])[:500],
                        "labels": labels,
                    }
                )

        entries.sort(key=lambda item: item["timestamp"], reverse=True)
        return entries[:limit]

    def _to_iso(self, nanoseconds: Any) -> str:
        try:
            return datetime.fromtimestamp(int(nanoseconds) / 1e9, tz=UTC).isoformat()
        except (TypeError, ValueError):
            return ""
