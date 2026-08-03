"""Regression tests built from a **captured API-server payload**, not a fake.

`tests/fixtures/real_pods_kind_qa.json` is `kubectl get pods -n payments -o json`
taken verbatim from a `kind` cluster carrying nine deliberately broken
workloads, with only `managedFields` and similar noise removed. Every field the
inspector reads keeps the exact shape the API server produced.

**That is the whole point of this file, and it is worth being blunt about
why.** A full suite of 1,172 tests passed while the platform could not diagnose
an ImagePullBackOff. The defect was one `if` in the wrong order, and it survived
because every fixture supplied `status` as the single merged string `kubectl`
*prints* (`"ImagePullBackOff"`), while the API server returns `phase: Pending`
plus a separate `containerStatuses[].state.waiting.reason`. The fakes encoded
the same misunderstanding as the code, so no amount of additional testing
against them could ever fail.

More tests cannot find a bug that lives in the shared assumption between the
code and its fixtures. Only real input can. See `docs/QA_AUDIT_2026-08-03.md`.
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

FIXTURE = Path(__file__).parent / "fixtures" / "real_pods_kind_qa.json"


@pytest.fixture(scope="module")
def real_pods() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def analysed(real_pods: dict[str, Any]) -> dict[str, Any]:
    result = ProviderResult(
        success=True,
        data=real_pods,
        equivalent_command="kubectl get pods -n payments -o json",
    )
    return PodInspector().analyse([result], InvestigationScope(context="kind-qa"))


def status_of(analysed: dict[str, Any], prefix: str) -> str | None:
    for pod in analysed["problematic_pods"]:
        if pod["name"].startswith(prefix):
            return pod["status"]
    return None


class TestTheFixtureIsActuallyRealistic:
    """If these fail, the fixture has been regenerated from a fake and every
    test below has quietly stopped proving anything."""

    def test_the_api_server_reports_phase_and_reason_separately(self, real_pods):
        """The shape the old fixtures got wrong. `kubectl` merges these two
        into one printed column; the API does not."""
        ledger = next(p for p in real_pods["items"] if p["metadata"]["name"].startswith("ledger"))
        assert ledger["status"]["phase"] == "Pending"
        waiting = ledger["status"]["containerStatuses"][0]["state"]["waiting"]
        assert waiting["reason"] == "ImagePullBackOff"

    def test_it_contains_a_pod_whose_phase_and_reason_disagree(self, real_pods):
        """The discriminating case. A fixture where phase and container reason
        always agree cannot tell a correct implementation from the broken one,
        because reading either field first gives the same answer."""
        disagreeing = [
            p["metadata"]["name"]
            for p in real_pods["items"]
            for cs in (p["status"].get("containerStatuses") or [])
            if (cs.get("state", {}).get("waiting") or {}).get("reason")
            and p["status"].get("phase") == "Pending"
        ]
        assert disagreeing, "fixture no longer exercises the bug it exists for"

    def test_it_contains_a_healthy_pod(self, real_pods):
        """Detection tests are worthless without a negative control."""
        assert any(p["metadata"]["name"].startswith("healthy-api") for p in real_pods["items"])


class TestImagePullBackOffIsDiagnosed:
    """The headline miss: a textbook Kubernetes failure the platform could not
    name, while every automated gate was green."""

    def test_the_container_reason_wins_over_the_phase(self, analysed):
        assert status_of(analysed, "ledger") == "ImagePullBackOff"

    def test_it_is_not_flattened_to_pending(self, analysed):
        """The exact regression. `Pending` is *true* of this pod and useless:
        it is the half of the answer that names no action."""
        assert status_of(analysed, "ledger") != "Pending"

    def test_it_reaches_a_signal(self, analysed):
        """Detection that produces no signal changes no diagnosis. The status
        string only matters because this mapping exists."""
        signal_type, _ = POD_STATUS_SIGNALS[status_of(analysed, "ledger")]
        assert signal_type == SignalType.POD_IMAGE_PULL_FAILURE

    def test_the_transient_pull_state_is_detected_too(self, analysed):
        """`ErrImagePull` is what the kubelet reports for the first several
        seconds, before it settles into `ImagePullBackOff`. Both shadow a
        `Pending` phase identically, and `POD_STATUS_SIGNALS` has always mapped
        both — but `_detect_pod_status` could not return the transient one, so
        the mapping was unreachable.

        Caught by mutation: dropping `ErrImagePull` from the recognised reasons
        left the whole suite green, because the fixture had only ever captured
        a pod that had already settled. This pod was captured mid-window from
        the same cluster.
        """
        assert status_of(analysed, "transient-pull") == "ErrImagePull"
        signal_type, _ = POD_STATUS_SIGNALS["ErrImagePull"]
        assert signal_type == SignalType.POD_IMAGE_PULL_FAILURE

    def test_every_reported_status_can_become_a_signal(self, analysed):
        """A reason returned but mapped to nothing is a worse silence than not
        detecting it: the pod shows as problematic and contributes no finding.
        This is what keeps `CONTAINER_PROBLEM_REASONS` honest as it grows."""
        unmapped = [
            pod["status"]
            for pod in analysed["problematic_pods"]
            if pod["status"] not in POD_STATUS_SIGNALS
        ]
        assert not unmapped, f"statuses with no signal mapping: {sorted(set(unmapped))}"


class TestTheOtherRealFailures:
    def test_crash_looping_pods_are_still_detected(self, analysed):
        """These worked before the fix and must keep working. They are also
        *why* the hole stayed hidden: their phase is `Running`, which is not a
        problem status, so they were the only pods that ever reached the
        container-status loop."""
        assert status_of(analysed, "checkout") == "CrashLoopBackOff"
        assert status_of(analysed, "fraud-scorer") == "CrashLoopBackOff"

    def test_an_unschedulable_pod_is_still_pending(self, analysed):
        """The phase fallback, and the case that makes it correct: no kubelet
        has accepted these pods, so there are no container statuses to be more
        specific with. `Pending` is the whole truth here."""
        assert status_of(analysed, "archiver") == "Pending"
        assert status_of(analysed, "batch-trainer") == "Pending"

    def test_the_healthy_pod_is_not_flagged(self, analysed):
        """A detector that flags everything has found nothing."""
        assert status_of(analysed, "healthy-api") is None

    def test_the_counts_reflect_the_real_cluster(self, analysed):
        assert analysed["total_pods"] == 10
        assert analysed["healthy"] is False
