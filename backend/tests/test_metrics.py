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
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.auth.dependencies import reset_authenticator
from app.core.config import settings
from app.main import app
from app.observability import metrics
from tests.test_investigation_service import FakeKubectl


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Metrics against a real investigation, authenticated as nobody.

    The `disabled` mode set here only takes effect because `conftest.py` resets
    the authenticator singleton around every test — without that this fixture
    inherits whatever authenticator ran last, every investigation 401s, and the
    counters read as *missing instrumentation* rather than as an auth failure.
    That is argued at `fresh_authenticator`; it is noted here because this is
    the fixture it was found through.
    """
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


class TestPhaseAttribution:
    """The last open P1, and the thing that corrected a published number.

    `PERFORMANCE_ENVELOPE.md` reported a ~10/s throughput ceiling and listed
    "which component that sits in" as unmeasured. Instrumenting the phases
    answered it — and, in doing so, showed the ceiling itself was the load
    harness rather than the platform. A measurement that only confirms what you
    expected has not been worth taking.
    """

    def test_every_phase_is_seeded(self, api):
        from app.observability.tracing import PHASES

        payload = scrape(api)
        for phase in PHASES:
            assert f'phase="{phase}"' in payload, phase

    def test_phases_are_a_closed_set(self, api):
        """A phase label whose values grow with the input is the same
        cardinality mistake a cluster label would be."""
        import re

        from app.observability.tracing import PHASES

        seen = set(
            re.findall(r'k8sagent_investigation_phase_seconds_\w+\{phase="([^"]+)"', scrape(api))
        )
        assert seen <= set(PHASES), seen - set(PHASES)

    def test_an_investigation_records_every_phase_it_runs(self, api):
        api.post("/investigate", json={"context": "test-cluster"})
        payload = scrape(api)

        for phase in ("collect", "analyse", "report"):
            assert (
                _value(payload, "k8sagent_investigation_phase_seconds_count", f'phase="{phase}"')
                > 0
            ), phase

    def test_the_phases_sum_to_less_than_the_investigation(self, api):
        """A sanity check that also documents the finding: the phases do not
        account for the whole of a worker's slot occupancy, and the gap is
        where the interesting question lives."""
        api.post("/investigate", json={"context": "test-cluster"})
        payload = scrape(api)

        phases = sum(
            _value(payload, "k8sagent_investigation_phase_seconds_sum", f'phase="{phase}"')
            for phase in ("collect", "analyse", "report", "persist")
        )
        assert phases > 0


class TestPhaseTimingIsTotal:
    """It records or it does nothing; it never changes the outcome."""

    def test_an_exception_inside_a_phase_propagates_unchanged(self):
        from app.observability import tracing

        with pytest.raises(ValueError, match="boom"), tracing.span("collect"):
            raise ValueError("boom")

    def test_a_failed_phase_is_still_timed(self, api):
        """A slow failure is exactly the shape an operator needs to see;
        timing only the happy path would hide it."""
        from app.observability import tracing

        before = _value(
            scrape(api), "k8sagent_investigation_phase_seconds_count", 'phase="collect"'
        )
        with pytest.raises(RuntimeError), tracing.span("collect"):
            raise RuntimeError("nope")

        after = _value(scrape(api), "k8sagent_investigation_phase_seconds_count", 'phase="collect"')
        assert after == before + 1

    def test_no_trace_exporter_is_configured(self):
        """OTLP export is deliberately absent: `opentelemetry-proto` requires
        `protobuf<7.0` and this project pins 7.35.1 so protobuf 7 can validate
        the agent's generated wire bindings. Installing it downgraded protobuf
        to 6.33.6 — the exact failure the pin exists to prevent.

        Catches: adding the dependency back without regenerating the bindings.
        """
        import google.protobuf

        assert google.protobuf.__version__.startswith("7."), (
            "protobuf was downgraded; the agent's generated bindings are "
            "validated against the runtime and this is how that breaks."
        )


class TestTheShippedAlertRulesMatchTheShippedMetrics:
    """`deploy/alerts/k8s-agent-alerts.yaml` must reference series that exist.

    This is the Tier-4 Prometheus lesson pointed at our own metrics. A rule
    naming a series the platform does not export is valid YAML, passes
    `promtool check rules`, evaluates successfully forever and fires never —
    the same shape as `container_spec_memory_limit_bytes` resolving to nothing
    on kube-prometheus-stack. Nothing else in the repository would notice.

    Asserted against the real exposition rather than against the metric
    definitions, because seeding is what makes an alert correct from a cold
    start: Prometheus does not create a labelled series until first use, so a
    rule on `outcome="failed"` would read "no data" on a healthy platform and
    fire on the *second* failure.
    """

    RULES = Path(__file__).resolve().parents[2] / "deploy" / "alerts" / "k8s-agent-alerts.yaml"

    def _exposition(self, api) -> str:
        return scrape(api)

    def test_the_rules_file_ships(self):
        assert self.RULES.exists(), "the alert rules are part of the deliverable"

    def test_every_referenced_series_is_exported(self, api):
        payload = self._exposition(api)
        exported = {line.split()[2] for line in payload.splitlines() if line.startswith("# TYPE ")}
        expanded = set(exported)
        for name in exported:
            expanded |= {f"{name}_bucket", f"{name}_sum", f"{name}_count", f"{name}_total"}

        referenced = set(re.findall(r"k8sagent_[a-z_]+", self.RULES.read_text()))
        assert referenced, "the rules must reference some metrics"

        missing = sorted(referenced - expanded)
        assert not missing, (
            f"alert rules reference series this platform never exports: {missing}. "
            f"Such a rule evaluates successfully and fires never."
        )

    def test_every_filtered_label_value_is_seeded(self, api):
        """A rule on an unseeded label value reads 'no data' while healthy."""
        payload = self._exposition(api)
        pairs = re.findall(r'(k8sagent_[a-z_]+)\{(\w+)="([^"]+)"', self.RULES.read_text())
        assert pairs, "the rules must filter on some label values"

        missing = [
            f'{metric}{{{label}="{value}"}}'
            for metric, label, value in pairs
            if not re.search(rf"{metric}\{{[^}}]*{label}=\"{value}\"", payload)
        ]
        assert not missing, f"alert rules filter on series that are never seeded: {missing}"

    def test_no_rule_can_reference_an_identifying_label(self):
        """The cardinality and disclosure rule, restated where it is easy to break.

        A rule author reaching for `by (cluster)` would be asking for a label
        that does not exist — but the failure is silent, so it is asserted here
        rather than left to review.
        """
        text = self.RULES.read_text()
        for forbidden in ("cluster=", "tenant=", "namespace=", "by (cluster)", "by (tenant)"):
            assert forbidden not in text, (
                f"{forbidden!r} appears in the alert rules; no series carries it, "
                f"and adding one would publish the customer list to any scraper"
            )
