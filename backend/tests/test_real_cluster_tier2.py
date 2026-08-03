"""Detection breadth, against a second captured cluster payload.

`tests/fixtures/real_pods_tier2.json` is `kubectl get pods -n payments -o json`
from the audit cluster after adding one workload per failure shape the platform
could not name: a genuine runtime OOM kill, a malformed image reference, an
image that will never be pulled, a blocking init container, failed Job pods,
and a readiness probe that never passes.

Same discipline as `test_real_cluster_fixtures.py` and for the same reason: the
defects these cover were all invisible to hand-written fixtures, because a fake
is written from what the author believes the API returns. Every state below was
produced by a real kubelet.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.analysis.models import SignalType
from app.analysis.signal_rules import POD_STATUS_SIGNALS
from app.collectors.base import InvestigationScope
from app.kubernetes.pod_inspector import PodInspector
from app.providers.base import ProviderResult

FIXTURE = Path(__file__).parent / "fixtures" / "real_pods_tier2.json"


@pytest.fixture(scope="module")
def real_pods() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def analysed(real_pods: dict[str, Any]) -> dict[str, Any]:
    result = ProviderResult(
        success=True, data=real_pods, equivalent_command="kubectl get pods -n payments -o json"
    )
    return PodInspector().analyse([result], InvestigationScope(context="kind-qa"))


def status_of(analysed: dict[str, Any], prefix: str) -> str | None:
    for pod in analysed["problematic_pods"]:
        if pod["name"].startswith(prefix):
            return pod["status"]
    return None


def pod_named(real_pods: dict[str, Any], prefix: str) -> dict[str, Any]:
    return next(p for p in real_pods["items"] if p["metadata"]["name"].startswith(prefix))


class TestTheOutOfMemoryKillIsNotShadowed:
    """The one that needed a real kernel to find.

    A container killed for memory reports `waiting.reason: CrashLoopBackOff`
    *and* `lastState.terminated.reason: OOMKilled` simultaneously. Reading
    fields in order returned the loop and discarded the cause, so
    `POD_OOM_KILLED` — a signal with its own hypothesis and its own remediation
    — could not fire for the failure it exists to name.
    """

    def test_the_pod_is_reported_as_oom_killed(self, analysed):
        assert status_of(analysed, "memory-hog") == "OOMKilled"

    def test_the_fixture_really_contains_the_shadowing_case(self, real_pods):
        """Without both reasons present at once this proves nothing — a pod
        reporting only `OOMKilled` is detected correctly either way."""
        container = pod_named(real_pods, "memory-hog")["status"]["containerStatuses"][0]

        assert container["state"]["waiting"]["reason"] == "CrashLoopBackOff"
        assert container["lastState"]["terminated"]["reason"] == "OOMKilled"
        assert container["lastState"]["terminated"]["exitCode"] == 137

    def test_it_reaches_the_out_of_memory_signal(self, analysed):
        signal_type, _ = POD_STATUS_SIGNALS[status_of(analysed, "memory-hog")]
        assert signal_type == SignalType.POD_OOM_KILLED


class TestRepeatedInvestigationsAgree:
    """A crash-looping container spends part of its cycle running, and at that
    instant the only trace is `lastState.terminated.reason: Error`. Half the
    reads said `Error` and half said `CrashLoopBackOff`, so the audit watched
    one unchanged cluster produce two different root causes on consecutive
    runs. The restart count is the fact that does not oscillate.
    """

    def test_a_looping_pod_reads_as_looping_while_backing_off(self, analysed, real_pods):
        container = pod_named(real_pods, "checkout")["status"]["containerStatuses"][0]

        assert container["state"]["waiting"]["reason"] == "CrashLoopBackOff"
        assert status_of(analysed, "checkout") == "CrashLoopBackOff"

    def test_and_reads_the_same_while_running(self, real_pods):
        """The other half of the same container's cycle.

        Derived from the captured pod rather than caught, because with the
        backoff at 5m the running window is a few seconds every five minutes.
        The shape is not invented: this pod was observed live as
        `waiting=-, lastState=Error, restartCount=13` before the backoff
        lengthened, which is exactly what removing the waiting state leaves.

        Both halves must give one answer or the same cluster yields a
        different root cause depending on when it was read.
        """
        pod = json.loads(json.dumps(pod_named(real_pods, "checkout")))
        container = pod["status"]["containerStatuses"][0]
        del container["state"]["waiting"]
        container["state"]["running"] = {"startedAt": "2026-08-03T05:40:00Z"}

        assert container["lastState"]["terminated"]["reason"] == "Error"
        assert container["restartCount"] >= 2
        assert PodInspector()._detect_pod_status(pod) == "CrashLoopBackOff"

    def test_a_single_restart_is_not_yet_a_loop(self):
        """The threshold matters in the other direction: one restart is a blip,
        and calling it a crash loop would flag every pod that ever recovered."""
        pod = {
            "metadata": {"name": "blip", "namespace": "prod"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "app",
                        "restartCount": 1,
                        "state": {"running": {}},
                        "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                    }
                ],
            },
        }

        assert PodInspector()._detect_pod_status(pod) == "Error"

    def test_a_completed_container_is_not_a_loop(self):
        """Init containers and Job pods restart legitimately after exiting
        cleanly; counting those would flag healthy workloads."""
        pod = {
            "metadata": {"name": "batch", "namespace": "prod"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "app",
                        "restartCount": 9,
                        "state": {"running": {}},
                        "lastState": {"terminated": {"reason": "Completed", "exitCode": 0}},
                    }
                ],
            },
        }

        assert PodInspector()._detect_pod_status(pod) is None


class TestTheRemainingImageFaults:
    def test_a_malformed_reference_is_named(self, analysed):
        assert status_of(analysed, "bad-image-name") == "InvalidImageName"

    def test_a_never_pull_policy_with_no_local_image_is_named(self, analysed):
        assert status_of(analysed, "never-pull") == "ErrImageNeverPull"

    def test_both_reach_the_image_signal(self, analysed):
        for prefix in ("bad-image-name", "never-pull"):
            signal_type, _ = POD_STATUS_SIGNALS[status_of(analysed, prefix)]
            assert signal_type == SignalType.POD_IMAGE_PULL_FAILURE, prefix


class TestConfigurationFaultsAreTheirOwnFinding:
    def test_a_missing_configmap_is_named(self, analysed):
        """Previously `Pending`, and before that misreported as a missing
        container image. It is neither: it is a reference that does not
        resolve."""
        assert status_of(analysed, "notifier") == "CreateContainerConfigError"

    def test_it_is_not_an_image_signal(self, analysed):
        signal_type, _ = POD_STATUS_SIGNALS[status_of(analysed, "notifier")]
        assert signal_type == SignalType.POD_CONFIG_ERROR
        assert signal_type != SignalType.POD_IMAGE_PULL_FAILURE


class TestInitContainersAreRead:
    """An init container that cannot start blocks every container behind it,
    and the app containers report only `PodInitializing` — the symptom. These
    statuses were not read at all, so the pod reported its bare phase."""

    def test_the_blocking_init_container_is_detected(self, analysed):
        assert status_of(analysed, "init-blocked") == "CrashLoopBackOff"

    def test_the_app_container_only_reports_the_symptom(self, real_pods):
        """Why reading init statuses first is what makes this work."""
        pod = pod_named(real_pods, "init-blocked")
        app = pod["status"]["containerStatuses"][0]
        init = pod["status"]["initContainerStatuses"][0]

        assert app["state"]["waiting"]["reason"] == "PodInitializing"
        assert init["lastState"]["terminated"]["reason"] == "Error"


class TestAReadinessProbeThatNeverPasses:
    """`gateway` is Running with every container healthy and has never been
    Ready. It was detected only through the kubelet's `Unhealthy` event, and
    Kubernetes expires events after an hour — after which a permanently broken
    workload looked entirely healthy."""

    def test_the_pod_is_reported_as_not_ready(self, analysed):
        assert status_of(analysed, "gateway") == "NotReady"

    def test_it_is_only_medium_severity(self, analysed):
        """With no clock available, a pod still starting is indistinguishable
        from one that will never be ready. MEDIUM is what makes this safe to
        emit without a grace period — it corroborates rather than concludes."""
        _, severity = POD_STATUS_SIGNALS["NotReady"]

        assert str(severity) == "medium"

    def test_a_ready_pod_is_not_flagged(self, analysed):
        assert status_of(analysed, "healthy-api") is None


class TestFailedPodsAreVisible:
    def test_failed_job_pods_are_detected(self, analysed):
        assert status_of(analysed, "doomed-migration") == "Error"

    def test_a_failed_phase_with_no_container_detail_still_counts(self):
        """The catch-all. A pod removed out from under the kubelet may carry no
        container status at all, and phase `Failed` is unambiguous."""
        pod = {"metadata": {"name": "gone", "namespace": "prod"}, "status": {"phase": "Failed"}}

        assert PodInspector()._detect_pod_status(pod) == "Failed"

    def test_an_evicted_pod_names_the_eviction(self):
        """Reported at the pod level, not on any container — an evicted pod's
        containers frequently explain nothing."""
        pod = {
            "metadata": {"name": "evicted-0", "namespace": "prod"},
            "status": {"phase": "Failed", "reason": "Evicted"},
        }

        assert PodInspector()._detect_pod_status(pod) == "Evicted"
        signal_type, _ = POD_STATUS_SIGNALS["Evicted"]
        assert signal_type == SignalType.POD_EVICTED


class TestNothingLostAndNothingInvented:
    def test_every_status_still_maps_to_a_signal(self, analysed):
        unmapped = [
            pod["status"]
            for pod in analysed["problematic_pods"]
            if pod["status"] not in POD_STATUS_SIGNALS
        ]
        assert not unmapped, f"statuses with no signal mapping: {sorted(set(unmapped))}"

    def test_only_the_healthy_control_is_unflagged(self, analysed, real_pods):
        """Fourteen broken workloads and one healthy one. A detector that
        flags the healthy pod too has found nothing."""
        flagged = {pod["name"] for pod in analysed["problematic_pods"]}
        everything = {pod["metadata"]["name"] for pod in real_pods["items"]}

        assert everything - flagged == {
            name for name in everything if name.startswith("healthy-api")
        }
