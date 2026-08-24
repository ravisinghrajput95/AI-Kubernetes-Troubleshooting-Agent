"""Prometheus query client.

Never raises for an operational problem. An unreachable, unconfigured, or slow
Prometheus produces a result carrying the reason, which the collector turns into
unavailable evidence — the same treatment every other missing fact gets.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings
from app.evidence.models import EvidenceStatus


@dataclass(frozen=True)
class QueryResult:
    status: EvidenceStatus
    samples: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    query: str = ""

    @property
    def usable(self) -> bool:
        return self.status.usable

    def scalar(self, default: float | None = None) -> float | None:
        """First sample's value, for queries that return a single series."""
        if not self.samples:
            return default
        try:
            return float(self.samples[0]["value"])
        except (KeyError, TypeError, ValueError):
            return default


class PrometheusClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.prometheus_url).rstrip("/")
        self.timeout = timeout or settings.observability_timeout_seconds
        self.tenant_id = tenant_id if tenant_id is not None else settings.prometheus_tenant_id

    @property
    def headers(self) -> dict[str, str]:
        """`X-Scope-OrgID` when configured, nothing when not.

        Plain Prometheus has no tenancy and needs none. Mimir, Cortex and
        Thanos behind a multi-tenant gateway all use the same header as Loki,
        and refuse a query without it — so a deployment pointing
        `PROMETHEUS_URL` at one saw every query fail and every metric signal
        silently absent, which is the M9.1 defect shape arriving by a different
        route.
        """
        return {"X-Scope-OrgID": self.tenant_id} if self.tenant_id else {}

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def query(self, promql: str) -> QueryResult:
        """Instant query. Returns samples as `{labels, value}` dicts."""
        if not self.configured:
            return QueryResult(
                EvidenceStatus.NOT_APPLICABLE,
                detail="Prometheus is not configured (set PROMETHEUS_URL).",
                query=promql,
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/query", params={"query": promql}
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException:
            return QueryResult(
                EvidenceStatus.TIMEOUT,
                detail=f"Prometheus did not respond within {self.timeout:.0f}s.",
                query=promql,
            )
        except httpx.HTTPError as exc:
            logger.warning("Prometheus query failed: {error}", error=str(exc))
            return QueryResult(
                EvidenceStatus.UNAVAILABLE,
                detail=f"Prometheus is unreachable: {exc}",
                query=promql,
            )
        except ValueError:
            return QueryResult(
                EvidenceStatus.FAILED,
                detail="Prometheus returned a response that was not valid JSON.",
                query=promql,
            )

        if payload.get("status") != "success":
            return QueryResult(
                EvidenceStatus.FAILED,
                detail=f"Prometheus rejected the query: {payload.get('error', 'unknown')}",
                query=promql,
            )

        samples = self._parse(payload.get("data", {}))
        return QueryResult(
            EvidenceStatus.OK if samples else EvidenceStatus.EMPTY,
            samples=samples,
            query=promql,
        )

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        parsed = []
        for entry in data.get("result", []):
            value = entry.get("value") or entry.get("values", [[None, None]])[-1]
            parsed.append(
                {
                    "labels": entry.get("metric", {}),
                    "value": value[1] if isinstance(value, list) and len(value) > 1 else None,
                }
            )
        return parsed
