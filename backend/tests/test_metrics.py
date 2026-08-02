"""What the platform says about itself, and what it must never say.

`PRODUCTION_READINESS.md` called the absence of self-observability "ironic for
an observability tool", and `docs/PERFORMANCE_ENVELOPE.md` made it concrete: it
tells an operator to size on throughput and alarm on queue depth, and the
platform exposed neither.

The tests that matter most here are the negative ones. A metric that leaks a
tenant's cluster names to anyone who can scrape the port would undo, in one
label, what M6 spent a milestone building — and it would do so silently,
because a leaking metric looks exactly like a working one.
"""

import re
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.core.config import settings
from app.main import app
from app.observability import metrics
from tests.test_investigation_service import FakeKubectl


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
    monkeypatch.setattr(executor_module.KubectlExecutor, "failing_resources", set(), raising=False)
    monkeypatch.setattr(settings, "auth_mode", "disabled")
    monkeypatch.setattr(settings, "allow_insecure_no_auth", True)
    monkeypatch.setattr(settings, "impersonate_users", False)
    with TestClient(app) as client:
        yield client


def scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def series_names(payload: str) -> set[str]:
    return {
        line.split("{")[0].split(" ")[0]
        for line in payload.splitlines()
        if line and not line.startswith("#")
    }


def labels_in(payload: str) -> set[str]:
    """Every label *name* present anywhere in the exposition."""
    found: set[str] = set()
    for line in payload.splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        inner = line[line.index("{") + 1 : line.rindex("}")]
        for pair in re.findall(r'(\w+)="', inner):
            found.add(pair)
    return found


class TestNothingIdentifyingIsALabel:
    """The rule the whole module is shaped around.

    Cardinality and disclosure both forbid it: one series per cluster across a
    thousand-cluster fleet is how a Prometheus falls over, and labelling by
    tenant publishes the customer list to any scraper.
    """

    FORBIDDEN: ClassVar[set[str]] = {
        "cluster",
        "cluster_id",
        "tenant",
        "tenant_id",
        "namespace",
        "user",
        "subject",
        "owner",
        "investigation",
        "investigation_id",
        "worker",
        "worker_id",
        "context",
    }

    def test_no_identifying_label_is_declared(self, api):
        """Catches: adding `cluster` to any metric in `app/observability`."""
        present = labels_in(scrape(api))
        assert not (present & self.FORBIDDEN), (
            f"{sorted(present & self.FORBIDDEN)} appears as a metric label. Every "
            f"one of these is unbounded across a fleet and identifies a customer."
        )

    def test_the_label_check_can_actually_see_labels(self, api):
        """A parser that found nothing would make the test above vacuous."""
        assert "outcome" in labels_in(scrape(api))

    def test_a_cluster_name_does_not_reach_the_exposition(self, api):
        """End to end, not by inspecting the declarations.

        An investigation runs against a named cluster; that name must appear
        nowhere in what a scraper receives.
        """
        api.post("/investigate", json={"context": "acme-prod-eu-west-1"})

        assert "acme-prod-eu-west-1" not in scrape(api)

    def test_queue_depth_is_labelled_by_role_not_by_worker(self, api):
        """A worker id is unbounded over a deployment's life: every restart and
        every rollout would add a series that never goes away."""
        payload = scrape(api)
        queues = re.findall(r'k8sagent_queue_depth\{queue="([^"]+)"\}', payload)

        assert set(queues) <= {"shared", "worker"}


class TestGroundingReasonsAreCategories:
    """Grounding messages quote the model, which quotes cluster text.

    Using one as a label would hand an unbounded, attacker-influenced string to
    the metrics store — the same injection surface `app/ai` closes at the prompt
    boundary, reopened at the metrics boundary.
    """

    def test_a_hostile_reason_becomes_a_fixed_category(self):
        from app.ai.root_cause_analyzer import _rejection_category

        hostile = "Model returned no root cause for pod evil--{}\n injected"
        assert _rejection_category(hostile) == "empty_root_cause"

    def test_an_unrecognised_reason_is_other_not_the_reason(self):
        from app.ai.root_cause_analyzer import _rejection_category

        assert _rejection_category("something nobody anticipated") == "other"

    def test_the_analyzer_passes_the_reason_through_the_categoriser(self, api, monkeypatch):
        """The mutation that survived a first pass.

        Testing `_rejection_category` alone proves the categoriser works and
        says nothing about whether the call site uses it — and grounding never
        rejects in this suite, so the hostile string was never exercised. This
        forces a rejection carrying attacker-shaped prose and asserts it does
        not reach a scraper.
        """
        from app.ai.root_cause_analyzer import RootCauseAnalyzer
        from app.analysis.grounding import GroundingResult

        poison = "Model cited pod evil-{}-injected-label-name"
        monkeypatch.setattr(
            RootCauseAnalyzer,
            "_parse_llm_json",
            lambda self, content: {"root_cause": "x"},
        )
        monkeypatch.setattr(
            "app.analysis.grounding.GroundingValidator.validate",
            lambda self, payload, analysis: GroundingResult(valid=False, reason=poison),
        )
        monkeypatch.setattr(
            "app.ai.llm_client.LLMClient.complete",
            lambda self, messages: {"success": True, "content": "{}"},
        )

        api.post("/investigate", json={"context": "test-cluster"})
        payload = scrape(api)

        assert poison not in payload
        assert "evil-" not in payload
        assert _value(payload, "k8sagent_grounding_rejections_total", 'reason="other"') > 0

    def test_every_category_is_seeded(self, api):
        """So an alert on a rejection reason is correct from a cold start."""
        payload = scrape(api)
        reasons = set(
            re.findall(r'k8sagent_grounding_rejections_total\{reason="([^"]+)"\}', payload)
        )

        assert "other" in reasons
        assert "contradiction" in reasons


class TestQueueDepthSampling:
    """The consumer's sampler, reached directly.

    It only runs in the distributed deployment, so the endpoint tests never
    exercise it — which let "label the queue by worker id" survive a mutation
    pass. A worker id is unbounded over a deployment's life: every restart and
    rollout adds a series that never goes away.
    """

    def _consumer(self, worker: str):
        from app.jobs.consumer import JobConsumer

        class Bus:
            prefix = "test"

            def queue_depth(self, worker_id: str = "") -> int:
                return 7 if worker_id else 3

        return JobConsumer(store=None, runner=None, bus=Bus(), worker_id=worker)

    def test_it_labels_by_role_not_by_worker_id(self, api):
        self._consumer("host-abc:4711")._sample_queues()
        payload = scrape(api)

        queues = set(re.findall(r'k8sagent_queue_depth\{queue="([^"]+)"\}', payload))
        assert queues == {"shared", "worker"}, queues
        assert "host-abc" not in payload

    def test_it_publishes_both_depths(self, api):
        self._consumer("w1")._sample_queues()
        payload = scrape(api)

        assert _value(payload, "k8sagent_queue_depth", 'queue="shared"') == 3
        assert _value(payload, "k8sagent_queue_depth", 'queue="worker"') == 7

    def test_a_broken_bus_does_not_kill_the_reaper(self, api):
        from app.jobs.consumer import JobConsumer

        class Broken:
            prefix = "test"

            def queue_depth(self, worker_id: str = "") -> int:
                raise RuntimeError("redis is gone")

        JobConsumer(store=None, runner=None, bus=Broken(), worker_id="w1")._sample_queues()


class TestTheEnvelopeIsObservable:
    """Every number `PERFORMANCE_ENVELOPE.md` tells an operator to act on."""

    @pytest.mark.parametrize(
        "metric",
        [
            "k8sagent_investigations_total",
            "k8sagent_investigations_submitted_total",
            "k8sagent_investigation_duration_seconds",
            "k8sagent_investigations_running",
            "k8sagent_worker_capacity",
            "k8sagent_queue_depth",
            "k8sagent_agents_connected",
            "k8sagent_cluster_access_total",
            "k8sagent_collection_duration_seconds",
            "k8sagent_evidence_records_total",
            "k8sagent_llm_calls_total",
            "k8sagent_diagnoses_total",
            "k8sagent_grounding_rejections_total",
        ],
    )
    def test_the_series_exists_before_anything_has_happened(self, api, metric):
        """Prometheus does not create a labelled series until first use, so an
        alert written against one reads "no data" while the platform is healthy
        and fires on the *second* occurrence. Seeding is what fixes that."""
        names = series_names(scrape(api))
        assert any(name.startswith(metric) for name in names), metric

    def test_saturation_is_computable(self, api):
        """running/capacity is the ratio an operator scales on; both halves
        have to be present for it to be a ratio at all."""
        names = series_names(scrape(api))
        assert "k8sagent_investigations_running" in names
        assert "k8sagent_worker_capacity" in names


class TestInstrumentationIsRecorded:
    def test_an_investigation_moves_the_counters(self, api):
        before = scrape(api)
        api.post("/investigate", json={"context": "test-cluster"})
        after = scrape(api)

        assert _value(after, "k8sagent_cluster_access_total", 'provider="kubeconfig"') > _value(
            before, "k8sagent_cluster_access_total", 'provider="kubeconfig"'
        )

    def test_evidence_status_is_counted(self, api):
        api.post("/investigate", json={"context": "test-cluster"})

        assert _value(scrape(api), "k8sagent_evidence_records_total", 'status="ok"') > 0

    def test_collection_duration_is_observed(self, api):
        api.post("/investigate", json={"context": "test-cluster"})
        payload = scrape(api)

        assert _value(payload, "k8sagent_collection_duration_seconds_count", "") > 0


class TestInstrumentationCannotBreakTheThingItMeasures:
    """An observability bug must not become an outage."""

    def test_a_failing_metric_is_swallowed(self, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("registry is on fire")

        monkeypatch.setattr(metrics.investigations_total, "labels", explode)
        metrics.investigation_finished("succeeded", 1.0)  # must not raise

    def test_a_failing_gauge_is_swallowed(self, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("nope")

        monkeypatch.setattr(metrics.investigations_running, "set", explode)
        metrics.running(3)


class TestTheEndpoint:
    def test_it_needs_no_credential(self, monkeypatch, tmp_path):
        """A scraper is infrastructure: it has no tenant, so there is no role
        for it to hold. Safe only because nothing here identifies anyone."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "api_tokens", "tok:alice@example.com")
        from app.auth.dependencies import reset_authenticator

        reset_authenticator()
        try:
            with TestClient(app) as client:
                assert client.get("/investigations").status_code == 401
                assert client.get("/metrics").status_code == 200
        finally:
            reset_authenticator()

    def test_it_can_be_turned_off(self, api, monkeypatch):
        monkeypatch.setattr(settings, "metrics_enabled", False)
        assert api.get("/metrics").status_code == 404

    def test_it_is_openmetrics(self, api):
        assert "openmetrics-text" in api.get("/metrics").headers["content-type"]

    def test_it_exposes_only_this_platforms_series(self, api):
        """A registry of our own, not the process-global default — which
        auto-registers process and GC collectors and is shared with anything
        else that imports the library."""
        names = series_names(scrape(api))
        foreign = {name for name in names if not name.startswith("k8sagent")}

        assert not foreign, f"/metrics is publishing {sorted(foreign)}"


def _value(payload: str, metric: str, labels: str) -> float:
    for line in payload.splitlines():
        if line.startswith("#"):
            continue
        head, _, raw = line.partition(" ")
        if not head.startswith(metric):
            continue
        if labels and labels not in head:
            continue
        if not labels and "{" in head:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return 0.0
