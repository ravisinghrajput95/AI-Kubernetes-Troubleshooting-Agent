"""Redaction must happen at the collection boundary.

Prior to the evidence layer, redaction ran only when building the LLM prompt,
so raw payloads still reached persisted reports and the HTTP API. These tests
pin the stronger guarantee: nothing un-redacted ever enters the store.
"""

from app.collectors.base import CollectionContext, InvestigationScope
from app.collectors.registry import CollectorRegistry
from app.collectors.scheduler import CollectionScheduler
from app.evidence.models import Evidence, EvidenceStatus
from app.providers.local_kubectl import LocalKubectlProvider
from tests.test_collection_scheduler import RecordingCollector


async def collect_payload(payload):
    async def behaviour(context, collector):
        return [
            Evidence.create(
                kind=collector.kind,
                status=EvidenceStatus.OK,
                target=context.scope.cluster_ref,
                data=payload,
                collector_id=collector.id,
            )
        ]

    collector = RecordingCollector("secretive", "k.secret", behaviour=behaviour)
    context = CollectionContext(
        scope=InvestigationScope(context="test"),
        provider=LocalKubectlProvider(context="test"),
    )
    store = await CollectionScheduler(CollectorRegistry([collector])).run(context)
    return store.data("k.secret"), store.first("k.secret")


async def test_secret_bearing_log_lines_are_redacted_in_the_store():
    data, evidence = await collect_payload(
        {"logs": ["ERROR auth failed password=hunter2", "token: abc123xyz"]}
    )

    assert evidence.redacted is True
    assert "hunter2" not in str(data)
    assert "abc123xyz" not in str(data)
    assert "[REDACTED]" in str(data)


async def test_sensitive_keys_are_redacted_by_name():
    data, _ = await collect_payload(
        {"env": {"DATABASE_PASSWORD": "s3cr3t", "api_key": "ak_live_1", "replicas": 3}}
    )

    assert data["env"]["DATABASE_PASSWORD"] == "[REDACTED]"
    assert data["env"]["api_key"] == "[REDACTED]"
    assert data["env"]["replicas"] == 3


async def test_nested_structures_are_redacted():
    data, _ = await collect_payload(
        {"pods": [{"containers": [{"env": [{"name": "TOKEN", "value": "token=leak"}]}]}]}
    )

    assert "leak" not in str(data)
