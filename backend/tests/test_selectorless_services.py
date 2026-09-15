"""A Service with no selector is a finding only when nothing routes it.

Read off a console sweep against a kind cluster: every whole-cluster
investigation carried `network.no_selector` for `default/kubernetes` — the
Service the API server maintains for itself on every cluster — and the fleet
page listed it as the same failure on two clusters.
"""

from app.analysis.engine import AnalysisEngine
from app.analysis.models import SignalType
from app.kubernetes.network_inspector import NetworkInspector
from app.providers.base import ProviderResult


class Scope:
    namespace = None


def service(namespace, name, selector=None):
    spec = {"type": "ClusterIP", "ports": [{"port": 443}]}
    if selector:
        spec["selector"] = selector
    return {"metadata": {"namespace": namespace, "name": name}, "spec": spec}


def endpoints(namespace, name, *addresses):
    body = {"metadata": {"namespace": namespace, "name": name}}
    if addresses:
        body["subsets"] = [{"addresses": [{"ip": ip} for ip in addresses]}]
    return body


def signals(services, endpoint_objects):
    network = NetworkInspector().analyse(
        [
            ProviderResult(success=True, data={"items": services}),
            ProviderResult(success=True, data={"items": endpoint_objects}),
        ],
        Scope(),
    )
    return AnalysisEngine().analyze({"network": network}).by_type(SignalType.NETWORK_NO_SELECTOR)


def test_the_api_servers_own_service_is_not_a_finding():
    found = signals(
        [
            service("default", "kubernetes"),
            service("kube-system", "kube-dns", {"k8s-app": "kube-dns"}),
        ],
        [
            endpoints("default", "kubernetes", "172.18.0.2"),
            endpoints("kube-system", "kube-dns", "10.0.0.5"),
        ],
    )

    assert not found


def test_a_selectorless_service_with_nothing_behind_it_still_is():
    found = signals(
        [
            service("payments", "legacy-db"),
            service("kube-system", "kube-dns", {"k8s-app": "kube-dns"}),
        ],
        [endpoints("payments", "legacy-db"), endpoints("kube-system", "kube-dns", "10.0.0.5")],
    )

    assert [signal.target.name for signal in found] == ["legacy-db"]
