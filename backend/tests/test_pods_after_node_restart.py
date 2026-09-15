"""A node restart is not a crash loop.

`tests/fixtures/real_pods_after_node_restart.json` is `kubectl get pods -A -o
json` from a kind cluster taken 24 minutes after its host restarted, with only
managed fields and annotations removed. Every container on the node came back
with `lastState.terminated.reason: Unknown, exitCode: 255` and one more
restart; the cluster had restarted twice in its life, so most counts were 2.

The inspector counted two restarts plus any non-`Completed` last termination
as `CrashLoopBackOff`. The investigation of that cluster listed the API server,
etcd, CoreDNS, kube-proxy and both `healthy-api` pods as crash-looping, and
said Service `payments/healthy-api` had "no healthy backend: all 2 pods it
selects are failing" while `kubectl` printed both `1/1 Running`.

The oracle here is kubectl's own reading of the same payload, recorded when it
was captured, so the test is not the code agreeing with itself.
"""

import copy
import json
from pathlib import Path

import pytest

from app.collectors.base import InvestigationScope
from app.kubernetes.pod_inspector import PodInspector
from app.providers.base import ProviderResult

FIXTURE = Path(__file__).parent / "fixtures" / "real_pods_after_node_restart.json"

# `kubectl get pods -A` for this payload, at capture time.
KUBECTL_SAYS_HEALTHY = {
    "coredns-589f44dc88-6gh4t",
    "coredns-589f44dc88-mhvqr",
    "etcd-k8s-agent-dev-control-plane",
    "kindnet-9xzdr",
    "kube-apiserver-k8s-agent-dev-control-plane",
    "kube-controller-manager-k8s-agent-dev-control-plane",
    "kube-proxy-f7l5q",
    "kube-scheduler-k8s-agent-dev-control-plane",
    "metrics-server-649dd47df4-gvzbh",
    "healthy-api-54c974858-c684f",
    "healthy-api-54c974858-km8ld",
    "local-path-provisioner-855c7b7774-bbqbk",
}
STILL_BROKEN = {
    "checkout-5b5fd56dbf-4cnmv": "CrashLoopBackOff",
    "checkout-5b5fd56dbf-zm4ws": "CrashLoopBackOff",
    "secret-reader-7b79d59f9d-klmfz": "CrashLoopBackOff",
    "fraud-scorer-75cbd9fb69-kcv7p": "OOMKilled",
    "ledger-55ffdd65fb-mjnvw": "ImagePullBackOff",
    "notifier-6fbf6f6dc-v89dg": "CreateContainerConfigError",
    "gateway-765557fcc4-ljj5b": "NotReady",
}


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


def statuses(payload: dict) -> dict[str, str]:
    analysed = PodInspector().analyse(
        [ProviderResult(success=True, data=payload)], InvestigationScope(context="kind")
    )
    return {pod["name"]: pod["status"] for pod in analysed["problematic_pods"]}


def container(payload: dict, pod_name: str) -> dict:
    pod = next(p for p in payload["items"] if p["metadata"]["name"] == pod_name)
    return pod["status"]["containerStatuses"][0]


def test_the_fixture_is_the_shape_that_misfired(payload):
    # Vacuity: without restarted, Ready containers whose last termination was
    # the node going away, every assertion below passes against the defect.
    restarted = [
        c
        for p in payload["items"]
        for c in p["status"].get("containerStatuses", [])
        if c["ready"]
        and c["restartCount"] >= 2
        and c["lastState"]["terminated"]["reason"] == "Unknown"
    ]
    assert len(restarted) >= 8
    assert (
        container(payload, "local-path-provisioner-855c7b7774-bbqbk")["lastState"]["terminated"][
            "reason"
        ]
        == "Error"
    )


def test_nothing_kubectl_calls_running_is_reported_broken(payload):
    reported = statuses(payload)
    assert not KUBECTL_SAYS_HEALTHY & set(reported), {
        name: reported[name] for name in KUBECTL_SAYS_HEALTHY & set(reported)
    }


def test_every_real_fault_is_still_reported(payload):
    # The control: a fix that stopped reporting restarts at all would pass the
    # test above and this one would catch it.
    reported = statuses(payload)
    for name, status in STILL_BROKEN.items():
        assert reported.get(name) == status, (name, reported.get(name))


def test_error_history_counts_until_the_container_has_outlived_backoff(payload):
    """Stability is proven from the read, not assumed from Ready.

    Move the rest of the read to five minutes after local-path-provisioner
    started: its four `Error` restarts are then recent, and the kubelet would
    not yet have reset its backoff, so it must be reported.
    """
    recent = copy.deepcopy(payload)
    started = container(recent, "local-path-provisioner-855c7b7774-bbqbk")["state"]["running"][
        "startedAt"
    ]  # 2026-09-15T05:38:24Z

    def clamp(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in {"startedAt", "finishedAt", "lastTransitionTime", "creationTimestamp"}
                    and value
                    and value > started
                ):
                    node[key] = "2026-09-15T05:43:24Z" if value > "2026-09-15T05:43:24Z" else value
                else:
                    clamp(value)
        elif isinstance(node, list):
            for item in node:
                clamp(item)

    clamp(recent)
    assert statuses(recent).get("local-path-provisioner-855c7b7774-bbqbk") == "CrashLoopBackOff"
