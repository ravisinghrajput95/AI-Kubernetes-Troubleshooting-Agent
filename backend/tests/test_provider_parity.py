"""Can an agent serve every read the platform knows how to ask for?

F7 names "API version assumptions with no discovery" and points at EndpointSlice
and Ingress. Running it turned up something worse in the same place. There are
two lookup tables between a collector and a cluster — the agent's evidence kinds
(`agent/internal/policy/kinds.go`, mirrored by `_KINDS` in
`app/providers/remote_agent.py`) and the collector's own resource name — and
nothing checked that they agree. Nine of the deep-investigation reads named a
resource the agent has no kind for, including `configmap` **singular** while the
table carries the plural, and `ingresses` while the table carries `ingress`.

The failure is quiet by design, which is why it lasted. `spec_for` refuses the
request, the collector records a non-usable evidence record saying the agent
does not know the kind, and the investigation *succeeds* with a gap. A
kubeconfig cluster gets the finding; an agent cluster does not; nothing compares
the two. The same shape as the M8a regression, and as the four Prometheus
queries that parsed, returned `success`, and matched nothing.

So this asks the collectors themselves what they read, by running every one of
them against a recording provider, and holds the answer against `kind_for`. A
collector added later is covered without anyone remembering to cover it — which
is the only kind of parity check worth having.
"""

import asyncio
import inspect

import pytest

from app.collectors import targeted
from app.collectors.base import CollectionContext, InvestigationScope
from app.collectors.kubernetes import build_default_collectors
from app.evidence.models import ResourceRef
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest
from app.providers.remote_agent import kind_for

SCOPE = InvestigationScope(context="prod", namespace="payments")
TARGET = ResourceRef(kind="Pod", name="web-0", namespace="payments")

# Reads the agent deliberately does not serve, each with the reason.
#
# An exception here is a decision, not a to-do list. `describe` is *kubectl's
# renderer*, not an API read, and reproducing its output in Go is the mistake
# `ResourceMetricsCollector` already refused to make for `kubectl top` — where
# the answer was to normalise both providers into one shape on the platform.
# Doing the same for Secrets is real design on a security-sensitive path: the
# platform reads a Secret through `describe` precisely because it prints key
# names and never values. Until that is designed, an agent cluster records a
# non-usable evidence record for it, which is the honest outcome and is what
# `_secret_keys` already handles.
UNSERVED: dict[tuple[str, str], str] = {
    ("describe", "secret"): (
        "describe is kubectl's renderer, not an API read; serving Secret key "
        "names through an agent needs a kind of its own, not a text format for "
        "Go to reproduce."
    ),
}


class Recorder:
    """A provider that answers nothing and remembers everything it was asked."""

    cluster_id = "prod"

    def __init__(self) -> None:
        self.requests: list[ResourceRequest] = []
        self.executed_commands: list[str] = []
        self.truncations: list[dict] = []

    async def fetch(self, request):
        return (await self.fetch_many([request]))[0]

    async def fetch_many(self, requests):
        self.requests.extend(requests)
        # Empty but successful, so a collector proceeds to whatever it reads
        # next instead of short-circuiting on its first failure.
        return [ProviderResult(success=True, data={"items": []}, text="") for _ in requests]


def every_collector():
    """Baseline collectors plus every targeted one a playbook can emit.

    Constructed directly rather than by running investigations that happen to
    trigger each playbook: a fixture firing only the crashloop playbook would
    report the network and storage collectors as covered when they were simply
    never run.
    """
    collectors = list(build_default_collectors())
    for _name, obj in vars(targeted).items():
        if not inspect.isclass(obj) or obj.__module__ != targeted.__name__:
            continue
        # `prefix` is what a concrete targeted collector names its evidence.
        # The two base classes leave it empty, and instantiating one produces a
        # read of resource `""` that belongs to no collector.
        if not getattr(obj, "prefix", ""):
            continue
        try:
            collectors.append(obj(TARGET))
        except TypeError:
            continue
    return collectors


# `ConfigReferenceCollector` reads a ConfigMap and a Secret *named by a pod
# spec*, so with an empty store it declines before issuing either. Those two
# reads are the ones the gap was found in, so the store is seeded rather than
# left to chance — an empty store here would have reported them as covered.
POD_SPEC = {
    "containers": [
        {"config_refs": [{"kind": "ConfigMap", "name": "web-config", "key": "DB_HOST"}]}
    ],
    "volumes": [{"type": "Secret", "name_ref": "web-tls"}],
}


def reads_issued() -> list[ResourceRequest]:
    from app.evidence.models import Evidence, EvidenceKind, EvidenceStatus
    from app.evidence.store import EvidenceStore

    recorder = Recorder()
    store = EvidenceStore()
    store.add(
        Evidence.create(
            kind=EvidenceKind.POD_SPEC,
            status=EvidenceStatus.OK,
            target=TARGET,
            data=POD_SPEC,
        )
    )
    context = CollectionContext(scope=SCOPE, provider=recorder, store=store)

    async def drive():
        for collector in every_collector():
            try:
                await collector.collect(context)
            except Exception:
                # A collector that cannot finish against an empty cluster has
                # still declared its reads by the time it fails.
                continue

    asyncio.run(drive())
    return recorder.requests


def distinct_reads() -> dict[tuple[str, str], ResourceRequest]:
    return {(str(r.verb), r.resource): r for r in reads_issued()}


def test_the_collectors_really_read_something():
    """The guard against this whole file passing vacuously.

    A recorder that saw nothing would satisfy every assertion below. Same
    reason `verify_deployment.py` refuses a scrape check with zero targets:
    "no unhealthy targets" is not a result.
    """
    reads = distinct_reads()
    assert len(reads) >= 15, f"only {len(reads)} distinct reads recorded: {sorted(reads)}"
    assert ("get", "pods") in reads
    assert ("get", "configmap") in reads, "the inline singular reads were not exercised"
    assert ("describe", "secret") in reads, "the Secret read was not exercised"
    assert ("get", "") not in reads, "a base class was instantiated as if it were a collector"


@pytest.mark.parametrize("read", sorted(distinct_reads()))
def test_an_agent_can_serve_every_read_the_platform_issues(read):
    request = distinct_reads()[read]
    if read in UNSERVED:
        assert kind_for(request) is None, (
            f"{read} is listed as unserved but the agent now has a kind for it. "
            f"Remove it from UNSERVED — a stale exception hides the next gap."
        )
        return

    assert kind_for(request) is not None, (
        f"The platform issues {read[0]} {read[1]!r} and no agent kind serves it, "
        f"so every agent-reached cluster records a gap where a kubeconfig one "
        f"gets a finding. Add it to `_KINDS` and to "
        f"`agent/internal/policy/kinds.go`, or list it in UNSERVED with the reason."
    )


def test_the_table_is_keyed_on_what_a_collector_actually_writes():
    """`_KINDS` is keyed on a resource string somebody typed by hand.

    `pod` and `pods` were both mapped; `configmap` and `ingresses` were not,
    and the difference was invisible because a missing key degrades rather than
    raises.
    """
    from app.providers.remote_agent import _KINDS

    for verb, resource in distinct_reads():
        if (verb, resource) in UNSERVED or verb == "logs":
            continue
        assert (ReadVerb(verb), resource) in _KINDS, f"{verb} {resource!r} is not a key in _KINDS"


def test_every_kind_the_platform_can_name_is_one_the_agent_knows():
    """The mirror of the above, across the wire.

    `_KINDS` naming a kind `kinds.go` does not serve means the platform sends a
    spec the agent answers `NOT_APPLICABLE` — a gap whose cause is on the other
    side of the wire and invisible from this side of it.
    """
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "agent/internal/policy/kinds.go"
    served = set(re.findall(r'"(k8s\.[a-z.]+)":\s*\{', source.read_text()))
    assert len(served) >= 15, f"the kind table did not parse: {served}"
    served.add("k8s.logs")

    from app.providers.remote_agent import _KINDS

    missing = sorted(set(_KINDS.values()) - served)
    assert not missing, (
        f"_KINDS names {missing}, which agent/internal/policy/kinds.go does not "
        f"serve. The platform would ask for it and be refused."
    )


def test_a_refusal_names_who_was_refused_on_both_paths():
    """Parity of the *reason*, not just of the read.

    `app/kubernetes/access.py` exists to tell a locked door from a broken
    cluster, and it can only do that when the message names whose permissions
    closed the door. Through a kubeconfig, kubectl relays the API server's
    sentence. Through an agent it said `unknown` — because client-go reports
    that for every error on a raw request, and the agent reads raw on purpose
    so the schema cannot decode the error body either. The server's actual
    sentence is in the response body that `DoRaw` hands back alongside the
    error.

    A source tripwire rather than a behavioural test, deliberately: the
    behaviour is pinned in Go (`internal/collectors/status_test.go`) and live
    against a real cluster in `test_agent_transport.py`, and neither runs in the
    Python suite. This is what makes the coupling visible to someone editing
    from this side.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "agent/internal/collectors/collector.go"
    ).read_text()

    assert "statusMessage(body)" in source, (
        "the agent no longer reads the API server's message out of the error "
        "body, so every refusal reports client-go's 'unknown' placeholder and a "
        "permissions problem becomes indistinguishable from a broken cluster"
    )
    assert "isPlaceholder(message)" in source, (
        "the agent no longer filters client-go's 'unknown' placeholder, so it "
        "reaches the evidence record as if it were the server's reason"
    )
