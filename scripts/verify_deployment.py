#!/usr/bin/env python3
"""Assert that a *deployed* platform works, against the real things it talks to.

Run by `scripts/integration_verify.sh` and by the `integration-verify` CI job,
against a chart installed on kind with an ingress controller, a
prometheus-operator Prometheus, metrics-server, Postgres and Redis.

**Why this exists.** Every significant defect in this project's last seven
tiers was found by a person deciding to stand something up, never by a test and
never by CI. They share one shape: the code is correct, the unit test asserts
something true, and a *different product* — Prometheus's parser, nginx's
buffering, the kubelet's probe path, the operator's label selector — disagrees
with us at a boundary no in-process test can reach. `2f60f76` is the fourth of
that class: `/metrics` advertised OpenMetrics and emitted Prometheus text
format, so a real Prometheus rejected every scrape, both targets read `down`,
zero series were stored, and all 17 alert rules evaluated against nothing
forever. `curl` saw 200 and 16 KB of correct exposition. The test asserted the
header, and the header was true.

**The dividing line against the existing `K8S_AGENT_INTEGRATION` suite**: that
suite runs *our code* against real Postgres and Redis, in one process, and its
assertions are about our contracts. Everything here needs a second product to
agree with us, and can only be observed from outside the pod. If an assertion
can be made by importing `app`, it belongs in pytest, and pytest is faster and
more precise. If it needs Prometheus to have stored something, it belongs here.

**Every check carries an honesty guard**, because the recurring failure in this
repository's harnesses is not a wrong assertion, it is a vacuous one — "5
collections, 0 records" from an `AttributeError`, a drain scenario that PASSed
in 0.2s with nothing in flight, a chaos run with no control. A check that
cannot distinguish success from an absent subject fails here rather than
passing: zero scrape targets is not "no unhealthy targets", and an SSE stream
that lasted 40ms proves nothing about buffering.

Stdlib only, deliberately: this must run on a laptop against a cluster without
setting up the backend's virtualenv first.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERT_RULES = REPO_ROOT / "deploy" / "alerts" / "k8s-agent-alerts.yaml"

# Below this the alert-series check has lost its subject: the rules file was
# renamed, emptied, or the regex stopped matching, and "all 0 referenced series
# are present" would pass. 15 distinct series are referenced today; the floor
# is 13 so that removing a rule is a reviewed diff rather than a broken check.
MIN_REFERENCED_SERIES = 13

# How long the *platform* must have spent emitting events for the buffering
# question to be answerable at all. Deliberately a property of the run rather
# than of the machine: the first version floored the wall-clock stream duration
# at 0.75s, passed locally at 1.22s, and failed the first CI run at 0.73s on a
# perfectly healthy platform, because the runner was faster than the laptop.
MIN_SERVER_SPAN_SECONDS = 0.20
# Arrivals must span at least this fraction of the window the platform emitted
# over. Under nginx's default proxy_buffering the ratio is ~0 — every frame
# lands at once when the response ends — so anything short of a total collapse
# passes, and a machine being fast or slow moves both sides together.
SPREAD_RATIO = 0.5


# --------------------------------------------------------------------------
# Result plumbing
# --------------------------------------------------------------------------


@dataclass
class Results:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(f"{name}{f' — {detail}' if detail else ''}")
        print(f"  \033[32mPASS\033[0m  {name}" + (f"  ({detail})" if detail else ""))

    def bad(self, name: str, detail: str) -> None:
        self.failed.append((name, detail))
        print(f"  \033[31mFAIL\033[0m  {name}\n        {detail}")

    def check(self, name: str, condition: bool, detail: str, ok_detail: str = "") -> bool:
        if condition:
            self.ok(name, ok_detail)
            return True
        self.bad(name, detail)
        return False


R = Results()


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def guarded(name: str, check, *args, **kwargs):
    """Run a check group; turn an exception into a failure, never a traceback.

    A harness that crashes on the thing it is measuring reports nothing at all
    — no summary, no other findings, and an exit code whose meaning depends on
    where it happened to die. That is this repository's oldest harness failure
    wearing yet another face: `fleet_bench.py` printing "5 collections, 0
    records" from an `AttributeError`.

    It bit here too. The agent routing check read `usable` from an
    investigation that had been *refused*, where the key is absent, and
    `None > 0` ended the run mid-report — while correctly detecting the defect
    it was pointed at. The finding survived only because it had already been
    printed.
    """
    try:
        return check(*args, **kwargs)
    except Exception as exc:
        R.bad(
            f"{name} completed without erroring",
            f"the check itself raised {type(exc).__name__}: {exc}. That is a "
            f"harness defect, not a platform one — but it is reported as a "
            f"failure because a check that could not run has not passed.",
        )
        return None


# --------------------------------------------------------------------------
# HTTP through the ingress, with an explicit Host header
# --------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


class Ingress:
    """Requests to the ingress controller, routed by Host.

    Host-header routing rather than a port-forward on purpose: a port-forward is
    a background process that can die without saying so, and an assertion whose
    subject vanished is exactly the vacuous-pass failure this file is guarding
    against.
    """

    def __init__(self, addr: str, timeout: float = 30.0) -> None:
        addr = addr.removeprefix("http://")
        self.host, _, port = addr.partition(":")
        self.port = int(port or 80)
        self.timeout = timeout

    def _conn(self, timeout: float | None = None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)

    def request(
        self,
        method: str,
        path: str,
        vhost: str,
        token: str | None = None,
        body: dict | None = None,
    ) -> Response:
        headers = {"Host": vhost, "Accept": "*/*"}
        payload = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn = self._conn()
        try:
            conn.request(method, path, body=payload, headers=headers)
            raw = conn.getresponse()
            return Response(raw.status, {k.lower(): v for k, v in raw.getheaders()}, raw.read())
        finally:
            conn.close()

    def stream_sse(
        self, path: str, vhost: str, token: str, deadline: float
    ) -> tuple[list[tuple[float, str]], float, float]:
        """Read an SSE stream, timestamping every frame as it arrives.

        Returns (frames, opened_at, closed_at). A frame is one `data:` line and
        the moment its bytes reached this process — which is the only thing that
        can tell "streamed" from "buffered and flushed at the end".
        """
        conn = self._conn(timeout=deadline)
        frames: list[tuple[float, str]] = []
        opened = time.monotonic()
        try:
            conn.request(
                "GET",
                path,
                headers={
                    "Host": vhost,
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {token}",
                },
            )
            raw = conn.getresponse()
            if raw.status != 200:
                raw.read()
                return [], opened, time.monotonic()
            while time.monotonic() - opened < deadline:
                line = raw.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\n")
                if text.startswith("data:"):
                    frames.append((time.monotonic(), text[5:].strip()))
        except (TimeoutError, OSError):
            pass
        finally:
            conn.close()
        return frames, opened, time.monotonic()


class Prometheus:
    def __init__(self, ingress: Ingress, vhost: str) -> None:
        self.ingress = ingress
        self.vhost = vhost

    def api(self, path: str) -> dict:
        response = self.ingress.request("GET", path, self.vhost)
        if response.status != 200:
            raise RuntimeError(
                f"Prometheus {path} -> HTTP {response.status}: {response.text[:200]}"
            )
        return response.json()

    def query(self, expr: str) -> list[dict]:
        from urllib.parse import quote

        payload = self.api(f"/api/v1/query?query={quote(expr)}")
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus rejected {expr!r}: {payload}")
        return payload["data"]["result"]

    def scalar(self, expr: str) -> float | None:
        result = self.query(expr)
        return float(result[0]["value"][1]) if result else None

    def targets(self) -> list[dict]:
        return self.api("/api/v1/targets?state=any")["data"]["activeTargets"]


def kubectl(context: str, *args: str) -> str:
    return subprocess.run(
        ["kubectl", "--context", context, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def wait_for(predicate, timeout: float, interval: float = 2.0):
    """Poll until `predicate` returns something truthy, or give up."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # a dependency still coming up is not a failure yet
            last = exc
        if last and not isinstance(last, Exception):
            return last
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_agent_api_versions(context: str) -> None:
    """Does this cluster actually serve the group versions the agent assumes?

    F7. `agent/internal/policy/kinds.go` maps each evidence kind to a fixed API
    path — `apis/networking.k8s.io/v1`, `apis/discovery.k8s.io/v1`,
    `apis/storage.k8s.io/v1`, `apis/metrics.k8s.io/v1beta1` — and performs no
    discovery. On a cluster that serves a different version the read 404s, and
    the platform records unavailable evidence: correct behaviour, and an
    investigation that is quietly shallower than the same cluster read through
    a kubeconfig, because kubectl does its own discovery and would have found
    it.

    **A discovery client in the agent was considered and not built.** Every
    version in that table is GA on every currently supported Kubernetes
    release, so a resolver would compute the same path it already has, at the
    cost of a startup dependency on a call that can fail. What the assumption
    lacked was not machinery but *evidence*, and that is what this is: the
    table checked against a real cluster's discovery document, in the job that
    already stands one up. When a version does move, this fails on the release
    that moves it rather than in a customer's degraded investigation.

    `metrics.k8s.io` is exempt from the failure: metrics-server is frequently
    absent and its absence is a normal degradation the platform reports rather
    than a defect. It is still listed, so the run says which way it went.
    """
    source = REPO_ROOT / "agent/internal/policy/kinds.go"
    if not R.check(
        "the agent's kind table can be read",
        source.exists(),
        f"{source} is missing, so this check would pass without comparing anything",
    ):
        return

    # `group: "apis/<group>/<version>"` or `"api/v1"`, plus its plural.
    # Whitespace-tolerant, because gofmt breaks a long entry across lines and a
    # regex that only matched the one-line form silently skipped the two
    # metrics kinds — the check would have passed while comparing 22 of 24
    # entries. The count guard below is what makes that visible.
    entries = re.findall(
        r'\{\s*group:\s*"(?P<group>[^"]+)",\s*plural:\s*"(?P<plural>[^"]+)"',
        source.read_text(),
    )
    if not R.check(
        "the kind table parsed",
        len(entries) >= 20,
        f"only {len(entries)} entries parsed out of {source.name}; the regex has "
        f"drifted from the source and this check is comparing nothing",
        ok_detail=f"{len(entries)} entries",
    ):
        return

    served: dict[str, set[str]] = {}
    for group_path in sorted({group for group, _ in entries}):
        try:
            listing = json.loads(kubectl(context, "get", "--raw", "/" + group_path))
        except Exception:
            served[group_path] = set()
            continue
        served[group_path] = {
            resource.get("name", "") for resource in listing.get("resources") or []
        }

    optional = {"apis/metrics.k8s.io/v1beta1"}
    missing = [
        f"{group}/{plural}"
        for group, plural in sorted(set(entries))
        if plural not in served.get(group, set()) and group not in optional
    ]
    absent_optional = [group for group in optional if group in served and not served[group]]

    R.check(
        "every API version the agent hardcodes is served by this cluster",
        not missing,
        f"{len(missing)} of the agent's reads name a path this cluster does not "
        f"serve: {', '.join(missing)}. The agent performs no discovery, so each "
        f"one 404s and becomes an evidence gap that the kubeconfig path — which "
        f"discovers — would not have.",
        ok_detail=f"{len(set(entries))} reads across {len(served)} group versions"
        + (f"; {', '.join(absent_optional)} absent (optional)" if absent_optional else ""),
    )


def check_probes(context: str, namespace: str, release: str) -> None:
    """The probes must resolve to the endpoints Tier 5 built.

    The chart pointed both probes at `/health`, which is deliberately
    liveness-shaped and never goes false while draining, so `/health/ready`,
    `begin_drain()` and the whole shutdown ordering were correct code that a
    Helm deployment never called. Asserted against the **live Deployment**
    rather than the rendered template: `helm template` output is what the chart
    author believes, and the object the API server holds is what the kubelet
    polls.
    """
    section("Probes point at the readiness split (§21 defect 3)")
    spec = json.loads(kubectl(context, "-n", namespace, "get", "deploy", release, "-o", "json"))
    containers = spec["spec"]["template"]["spec"]["containers"]

    # Honesty guard: no container means every path assertion below is vacuous.
    if not R.check(
        "the Deployment has containers to probe",
        len(containers) == 1,
        f"expected exactly 1 container, found {len(containers)}",
        f"{containers[0]['name'] if containers else '-'}",
    ):
        return

    container = containers[0]
    for kind, expected in (("livenessProbe", "/health/live"), ("readinessProbe", "/health/ready")):
        probe = container.get(kind)
        if not R.check(f"{kind} is configured", bool(probe), f"{kind} is absent"):
            continue
        path = probe.get("httpGet", {}).get("path")
        R.check(
            f"{kind} -> {expected}",
            path == expected,
            f"{kind} polls {path!r}; /health is liveness-shaped and never goes "
            f"false while draining, so pointing readiness at it makes the whole "
            f"drain sequence inert",
            ok_detail=path,
        )

    # preStop is the other half: readiness alone cannot close the Endpoints
    # propagation window, because the probe is polled every 10s and the listener
    # is gone long before the next poll.
    lifecycle = container.get("lifecycle", {}).get("preStop")
    R.check(
        "a preStop hook covers the Endpoints propagation window",
        bool(lifecycle),
        "no preStop hook; the pod stops accepting while traffic is still routed to it",
        ok_detail=" ".join(lifecycle.get("exec", {}).get("command", [])) if lifecycle else "",
    )


def check_ingress(ingress: Ingress, vhost: str, token: str) -> None:
    """The Ingress serves the platform — health, metrics and the API.

    §21 recorded the Ingress as "created and correct, but no ingress controller
    was installed to serve it". An Ingress object that routes to nothing is
    valid YAML with no symptom, which is the same family as a ServiceMonitor
    nobody selects.
    """
    section("The Ingress serves the platform")

    health = ingress.request("GET", "/health", vhost)
    R.check(
        "GET /health through the ingress",
        health.status == 200,
        f"HTTP {health.status}: {health.text[:200]}",
        ok_detail=f"auth_mode={health.json().get('auth_mode') if health.status == 200 else '?'}",
    )

    live = ingress.request("GET", "/health/live", vhost)
    R.check("GET /health/live", live.status == 200, f"HTTP {live.status}: {live.text[:200]}")

    ready = ingress.request("GET", "/health/ready", vhost)
    if R.check(
        "GET /health/ready", ready.status == 200, f"HTTP {ready.status}: {ready.text[:200]}"
    ):
        checks = ready.json().get("checks", ready.json())
        R.check(
            "readiness consults the real dependencies",
            isinstance(checks, dict) and {"postgres", "redis"} <= set(checks),
            f"readiness reported {checks!r}; it must name postgres and redis, "
            f"or it is not consulting the store at all",
            ok_detail=json.dumps(checks),
        )

    # Authentication is live through the ingress, not bypassed by it.
    anon = ingress.request("GET", "/investigations", vhost)
    R.check(
        "an unauthenticated request is refused",
        anon.status == 401,
        f"expected 401, got HTTP {anon.status} — the ingress is reaching a "
        f"deployment that authenticates nobody",
    )

    me = ingress.request("GET", "/me", vhost, token=token)
    if R.check(
        "GET /me with a bearer token", me.status == 200, f"HTTP {me.status}: {me.text[:200]}"
    ):
        R.check(
            "the token resolves to the configured role",
            me.json().get("role") == "operator",
            f"/me reported {me.json()!r}; the harness runs as `operator` "
            f"deliberately — a caller with every permission cannot tell a "
            f"working permission table from an absent one",
            ok_detail=f"role={me.json().get('role')}",
        )


def check_metrics_endpoint(ingress: Ingress, vhost: str) -> None:
    """The body must be what the content type promises.

    This is `2f60f76` asserted at the wire, one layer below the Prometheus
    target check that is the real pin. Kept because it names the failure
    precisely when it fires, where a `down` target names only the symptom.
    """
    section("/metrics is what its Content-Type claims")
    response = ingress.request("GET", "/metrics", vhost)
    if not R.check(
        "GET /metrics through the ingress",
        response.status == 200,
        f"HTTP {response.status}: {response.text[:200]}",
    ):
        return

    content_type = response.headers.get("content-type", "")
    body = response.text
    R.check(
        "the exposition carries this platform's series",
        "k8sagent_" in body,
        "no k8sagent_ series in the exposition",
        ok_detail=f"{len(body)} bytes",
    )
    if "openmetrics" in content_type:
        R.check(
            "an OpenMetrics content type is served an OpenMetrics body",
            body.rstrip().endswith("# EOF"),
            "Content-Type says openmetrics and the body has no terminating "
            "`# EOF`. Prometheus selects its parser from the header and rejects "
            "the *whole* scrape with `data does not end with # EOF`.",
            ok_detail=content_type.split(";")[0],
        )
    else:
        R.check(
            "a text-format content type is served a text-format body",
            not body.rstrip().endswith("# EOF"),
            f"Content-Type is {content_type!r} but the body terminates with "
            f"`# EOF`, which only OpenMetrics does",
            ok_detail=content_type.split(";")[0],
        )


def check_scrape_target(prom: Prometheus, release: str) -> bool:
    """A real Prometheus must be scraping us successfully.

    The one assertion that would have caught `2f60f76` on its own. Everything
    local agreed the endpoint was fine; only a scraper parsing what the header
    promised disagreed.
    """
    section("The ServiceMonitor produces a healthy scrape target")

    def ours():
        found = [t for t in prom.targets() if release in t.get("scrapePool", "")]
        return found if found and all(t.get("health") == "up" for t in found) else None

    targets = wait_for(ours, timeout=120)
    if targets is None:
        targets = [t for t in prom.targets() if release in t.get("scrapePool", "")]

    # Honesty guard, and it is the load-bearing one. A ServiceMonitor that no
    # Prometheus selects yields *zero* targets — and "every one of zero targets
    # is up" passes. That is precisely the shape of the kube-prometheus-stack
    # `release:` label trap this harness reproduces on purpose.
    if not R.check(
        "the ServiceMonitor was selected and produced targets",
        bool(targets),
        f"Prometheus has no target whose scrapePool names {release!r}. The "
        f"ServiceMonitor exists and is valid; nothing selected it. "
        f"kube-prometheus-stack selects on `release:` by default — see "
        f"metrics.serviceMonitor.labels.",
        ok_detail=f"{len(targets or [])} targets in {targets[0]['scrapePool'] if targets else ''}",
    ):
        return False

    unhealthy = [
        (t["scrapeUrl"], t.get("health"), t.get("lastError", ""))
        for t in targets
        if t.get("health") != "up"
    ]
    R.check(
        "every scrape target is up",
        not unhealthy,
        "Prometheus could not scrape: "
        + "; ".join(f"{url} health={health} err={err!r}" for url, health, err in unhealthy),
        ok_detail=f"{len(targets)} up",
    )

    # `up` with a non-empty lastError is possible during recovery, and an error
    # Prometheus recovered from is still an error we shipped.
    errored = [(t["scrapeUrl"], t["lastError"]) for t in targets if t.get("lastError")]
    R.check(
        "no target reports a scrape error",
        not errored,
        "; ".join(f"{url}: {err!r}" for url, err in errored),
    )
    return not unhealthy and not errored


def referenced_series() -> set[str]:
    """The k8sagent_ series the shipped alert rules depend on."""
    return set(re.findall(r"k8sagent_[a-z_]+", ALERT_RULES.read_text()))


def check_alert_series_are_stored(prom: Prometheus) -> None:
    """Every series the alert rules reference must be *in Prometheus*.

    `tests/test_metrics.py` already asserts they are in the exposition, which is
    necessary and was not sufficient: with `2f60f76` present every one of them
    was in the exposition and none of them was in Prometheus. The rules would
    have evaluated against nothing, successfully, forever.

    Instant queries rather than `/api/v1/label/__name__/values`, because a name
    that was ingested once and never again still appears in the label values
    API. An instant query resolves through the 5-minute lookback, so it means
    "present with recent samples".
    """
    section("Every alert-rule series is stored in Prometheus")
    names = referenced_series()

    if not R.check(
        "the alert rules reference series",
        len(names) >= MIN_REFERENCED_SERIES,
        f"only {len(names)} k8sagent_ series referenced in {ALERT_RULES.name} "
        f"(expected at least {MIN_REFERENCED_SERIES}). Either the rules moved "
        f"or the regex stopped matching — and 'all 0 referenced series are "
        f"present' would have passed.",
        ok_detail=f"{len(names)} distinct series",
    ):
        return

    def absent() -> list[str]:
        missing = []
        for name in sorted(names):
            candidates = [name, f"{name}_bucket", f"{name}_count", f"{name}_total"]
            if not any(prom.scalar(f"count({candidate})") for candidate in candidates):
                missing.append(name)
        return missing

    # Prometheus needs a scrape or two before the first samples land, so poll
    # for the clean result and report whatever is still missing at the deadline.
    missing = absent() if wait_for(lambda: not absent(), timeout=90) is None else []

    R.check(
        "every referenced series is present in Prometheus",
        not missing,
        f"{len(missing)} of {len(names)} series the alert rules depend on are "
        f"not in Prometheus: {missing}. Such a rule evaluates successfully and "
        f"fires never.",
        ok_detail=f"{len(names)}/{len(names)} present",
    )


def run_investigation(
    ingress: Ingress, vhost: str, token: str
) -> tuple[str | None, list, float, float]:
    """Submit an investigation through the ingress and stream its progress."""
    section("An investigation runs end to end through the ingress")

    accepted = ingress.request(
        # `refresh` for the same reason as the agent leg: this check measures
        # incremental SSE delivery through a real proxy, and an investigation
        # served from cache finishes before there is anything to stream.
        "POST",
        "/investigations",
        vhost,
        token=token,
        body={"namespace": "kube-system", "refresh": True},
    )
    if not R.check(
        "POST /investigations is accepted",
        accepted.status == 202,
        f"HTTP {accepted.status}: {accepted.text[:300]}",
    ):
        return None, [], 0.0, 0.0

    job_id = accepted.json()["id"]
    frames, opened, closed = ingress.stream_sse(
        f"/investigations/{job_id}/events", vhost, token, deadline=180.0
    )

    def finished():
        state = ingress.request("GET", f"/investigations/{job_id}", vhost, token=token)
        if state.status != 200:
            return None
        return (
            state.json()
            if state.json().get("status") in {"succeeded", "failed", "cancelled"}
            else None
        )

    final = wait_for(finished, timeout=120, interval=2.0)
    if not R.check(
        "the investigation reaches a terminal state",
        final is not None,
        f"investigation {job_id} never terminated",
        ok_detail=(final or {}).get("status", ""),
    ):
        return job_id, frames, opened, closed

    R.check(
        "the investigation succeeded",
        final.get("status") == "succeeded",
        f"status={final.get('status')!r} error={final.get('error')!r}. Reads are "
        f"impersonated as the caller, so a total refusal here usually means the "
        f"platform's ServiceAccount lacks the `impersonate` verb (§21 defect 1).",
    )

    coverage = (final.get("investigation") or {}).get("evidence_coverage") or {}
    R.check(
        "the investigation collected real evidence",
        bool(coverage) and coverage.get("usable", 0) > 0,
        f"no usable evidence: {coverage!r}. A 'succeeded' investigation that "
        f"collected nothing is the vacuous pass this harness exists to refuse.",
        ok_detail=f"{coverage.get('usable')} usable of {coverage.get('total')} records",
    )

    # The rendered report, served by a worker that may not have rendered it.
    pdf = ingress.request("GET", f"/investigations/{job_id}/pdf", vhost, token=token)
    R.check(
        "the PDF report is served through the ingress",
        pdf.status == 200 and pdf.body.startswith(b"%PDF"),
        f"HTTP {pdf.status}, {len(pdf.body)} bytes, starts {pdf.body[:8]!r}",
        ok_detail=f"{len(pdf.body)} bytes",
    )
    return job_id, frames, opened, closed


def check_sse_is_incremental(frames: list, opened: float, closed: float) -> None:
    """SSE reaches the client incrementally, end to end, through a real proxy.

    What this verifies: the application streams, ASGI does not accumulate the
    response, the SSE framing survives the proxy, and events reach a client as
    the platform produces them rather than in one delivery at the end. That is
    the property the console's live timeline depends on, and it is asserted
    against a real ingress-nginx rather than `TestClient`, which buffers
    streamed responses and so verifies framing but never delivery.

    **What this does NOT verify, stated because the first version claimed it
    did: it does not pin `X-Accel-Buffering: no`.** That header
    (`app/api/investigate.py`) exists to defeat proxy buffering, and the check
    was written to catch its removal. It does not. Removing the header and
    re-running produced 33/33 with arrivals tracking emissions at 99% — twice,
    once with ingress-nginx's default `proxy_buffering off`, and again after
    turning buffering *on* for this vhost.

    Two reasons, and both are about nginx rather than about us. ingress-nginx
    ships `proxy_buffering off` globally, so the header is redundant on a
    default install. And `proxy_buffering on` does not hold a response until it
    completes — nginx forwards as buffers fill, so at this traffic shape (29
    frames, ~8 KB, over 1.3s) delivery looks the same either way. The shape
    where the header earns its keep is a long investigation emitting sparse
    events, which this harness cannot produce without an artificially slow
    investigation — and engineering the scenario to make the assertion true
    would be testing nginx's tuning, not our code.

    So the header stays, because other proxies do honour it and it costs
    nothing, and this check stops claiming to guard it. A mutation-surviving
    assertion that reads as a guarantee is worse than an honest description of
    a narrower one.

    **The comparison is against a control taken from the same run.** Every
    frame carries the server-side moment it was emitted, so the question is
    whether arrivals track emissions, and the threshold scales with the window
    the platform actually used.

    The first version floored the wall-clock stream duration at 0.75s instead.
    It passed locally at 1.22s and **failed the first CI run at 0.73s** on a
    healthy platform, because the runner was faster than the laptop. An
    absolute threshold on a machine-dependent quantity is a coin flip wearing a
    rigorous face.
    """
    section("SSE frames arrive incrementally through nginx")

    if not R.check(
        "the stream delivered frames",
        len(frames) >= 3,
        f"only {len(frames)} SSE frames arrived; nothing can be concluded about "
        f"buffering from fewer than 3",
        ok_detail=f"{len(frames)} frames",
    ):
        return

    emitted = []
    for _, payload in frames:
        try:
            stamp = json.loads(payload).get("at")
            emitted.append(datetime.fromisoformat(stamp).timestamp())
        except (ValueError, TypeError, AttributeError):
            continue

    if not R.check(
        "frames carry the moment the platform emitted them",
        len(emitted) >= 3,
        f"only {len(emitted)} of {len(frames)} frames carried a parseable `at`; "
        f"without the server-side timestamps there is no control to compare "
        f"arrivals against, and this check would be measuring nothing",
        ok_detail=f"{len(emitted)} timestamps",
    ):
        return

    server_span = max(emitted) - min(emitted)
    arrival_span = frames[-1][0] - frames[0][0]
    duration = closed - opened

    # The §18 lesson: a drain scenario reported PASS while the process exited
    # 0.2s after SIGTERM with nothing in flight. If the platform emitted every
    # event inside a few milliseconds there is nothing for buffering to smear,
    # so refuse to conclude rather than passing. This is the only absolute
    # threshold left, and it is on what the *platform* did rather than on how
    # fast the machine ran.
    if not R.check(
        "the platform emitted events over a measurable window",
        server_span >= MIN_SERVER_SPAN_SECONDS,
        f"all {len(emitted)} events were emitted within {server_span * 1000:.0f}ms "
        f"(floor {MIN_SERVER_SPAN_SECONDS}s). Buffered and unbuffered delivery "
        f"are indistinguishable over that window, so this check is refusing to "
        f"report a pass it did not earn. On a real cluster an investigation "
        f"emitting less than this is itself worth looking at.",
        ok_detail=f"{server_span:.2f}s",
    ):
        return

    R.check(
        "arrivals track emissions rather than landing in one blob",
        arrival_span >= SPREAD_RATIO * server_span,
        f"the platform emitted these {len(frames)} events over {server_span:.2f}s "
        f"and they all arrived within {arrival_span * 1000:.0f}ms of each other "
        f"— that is a buffered blob. nginx buffers proxied responses unless the "
        f"upstream sends `X-Accel-Buffering: no` "
        f"(app/api/investigate.py SSE_HEADERS).",
        ok_detail=f"{arrival_span:.2f}s of arrivals against {server_span:.2f}s of "
        f"emissions ({arrival_span / server_span:.0%}), first frame at "
        f"+{frames[0][0] - opened:.2f}s of a {duration:.2f}s stream",
    )


def check_counters_moved(prom: Prometheus, before: float | None) -> None:
    """Prometheus must have *stored* what the investigation produced.

    Series presence proves the names exist; seeding puts them there from a cold
    start by design. This is the check that proves the pipeline carries data:
    an investigation ran, and its effect is visible in Prometheus.
    """
    section("Prometheus stored what the investigation produced")
    if before is None:
        R.bad("a baseline was taken before the investigation", "no baseline; check skipped")
        return

    def increased() -> bool:
        current = prom.scalar("sum(k8sagent_investigations_total)")
        return current is not None and current > before

    grew = wait_for(increased, timeout=90) is not None
    after = prom.scalar("sum(k8sagent_investigations_total)")
    R.check(
        "the investigation counter increased in Prometheus",
        grew,
        f"sum(k8sagent_investigations_total) is still {after} (was {before}) "
        f"after an investigation completed. The series exists and Prometheus is "
        f"not storing what happens.",
        ok_detail=f"{before} -> {after}",
    )


def check_autoscaling(context: str, namespace: str, release: str) -> None:
    """The HPA must be able to read a metric, not merely exist.

    §21 listed the HPA as unexercised. An HPA whose current utilisation reads
    `<unknown>` never scales and reports no error — the same silent-inertness
    family as everything else here.
    """
    section("The HPA can read a metric")
    hpas = json.loads(kubectl(context, "-n", namespace, "get", "hpa", "-o", "json"))["items"]
    ours = [h for h in hpas if h["metadata"]["name"] == release]
    if not R.check(
        "the chart created an HPA",
        bool(ours),
        f"no HorizontalPodAutoscaler named {release!r} (found {[h['metadata']['name'] for h in hpas]})",
    ):
        return

    def has_metric():
        current = json.loads(kubectl(context, "-n", namespace, "get", "hpa", release, "-o", "json"))
        for metric in current.get("status", {}).get("currentMetrics") or []:
            value = metric.get("resource", {}).get("current", {})
            if value.get("averageUtilization") is not None:
                return value
        return None

    value = wait_for(has_metric, timeout=180, interval=5.0)
    R.check(
        "the HPA reports a current CPU utilisation",
        value is not None,
        "the HPA's currentMetrics never resolved — it reads <unknown> and will "
        "never scale. metrics-server is the usual cause.",
        ok_detail=f"averageUtilization={(value or {}).get('averageUtilization')}%",
    )


def enrol(ingress: Ingress, vhost: str, token: str, cluster_id: str, out: str) -> int:
    """Mint an enrolment and write the manifest the platform generates.

    A separate mode rather than part of the verification run, because it needs
    `admin` and the verification run deliberately holds only `operator`.

    The manifest is taken from the endpoint rather than kept in this repository
    on purpose. A checked-in copy would drift from what the platform emits, and
    the harness would then be verifying itself — the same reason the
    observability fixtures are captured from a live backend rather than written
    by hand.
    """
    response = ingress.request(
        "POST",
        "/agents/enrolment",
        vhost,
        token=token,
        body={"cluster_id": cluster_id, "ttl_minutes": 30},
    )
    if response.status != 201:
        print(f"enrolment failed: HTTP {response.status}: {response.text[:400]}")
        return 1

    payload = response.json()
    endpoint = payload.get("gateway_endpoint", "")

    # The failure this catches has no other symptom: `agent_gateway_advertise`
    # unset renders the endpoint as the literal `<platform-host>:9443`, so the
    # manifest an operator applies — and the console's /connect flow hands
    # them — carries a placeholder rather than an address. Third instance of
    # "the knob exists and the chart never turned it", after the probe paths
    # and the gateway's own DNS names.
    if "<" in endpoint or not endpoint:
        print(
            f"the enrolment names {endpoint!r} as the address to dial. That is a "
            f"placeholder, not an endpoint: AGENT_GATEWAY_ADVERTISE is unset, so "
            f"every manifest this deployment generates is unusable."
        )
        return 1

    Path(out).write_text(payload["manifest"])
    print(f"enrolled {cluster_id}; agents dial {endpoint}")
    return 0


def _investigate_on(context: str, namespace: str, pod: str, token: str, cluster_id: str) -> dict:
    """Submit an investigation *from inside* a named pod, and wait for it.

    `kubectl exec` rather than a port-forward: a port-forward is a background
    process that can die without saying so, and this check exists to observe
    which worker accepted a submission. It has to be certain which one did.
    """
    script = (
        "import json,urllib.request,time\n"
        "req=urllib.request.Request('http://127.0.0.1:8000/investigations',"
        # `refresh` because this check exists to observe the *stream* being
        # used. Without it the second and third submissions would be answered
        # from the collection cache, report `provider=agent` correctly, and
        # assert nothing about routing — a vacuous check, which is the failure
        # mode every guard in this file is written against.
        f"data=json.dumps({{'context':'{cluster_id}','namespace':'kube-system',"
        f"'refresh':True}}).encode(),"
        f"headers={{'Content-Type':'application/json','Authorization':'Bearer {token}'}})\n"
        "jid=json.load(urllib.request.urlopen(req))['id']\n"
        "d={}\n"
        "for _ in range(90):\n"
        "    r=urllib.request.Request('http://127.0.0.1:8000/investigations/'+jid,"
        f"headers={{'Authorization':'Bearer {token}'}})\n"
        "    d=json.load(urllib.request.urlopen(r))\n"
        "    if d.get('status') in ('succeeded','failed','cancelled'): break\n"
        "    time.sleep(2)\n"
        "inv=d.get('investigation') or {}\n"
        "cov=inv.get('evidence_coverage') or {}\n"
        "print(json.dumps({'status':d.get('status'),'error':d.get('error') or '',"
        "'provider':(inv.get('cluster_access') or {}).get('provider'),"
        "'usable':cov.get('usable'),'total':cov.get('total')}))"
    )
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            namespace,
            "exec",
            pod,
            "--",
            "python",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
    )
    for line in reversed(result.stdout.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"status": "harness-error", "error": (result.stderr or result.stdout)[:200]}


def check_agent_path(
    ingress: Ingress, vhost: str, token: str, context: str, namespace: str, cluster_id: str
) -> None:
    """The agent link, end to end: mTLS enrolment, presence, and M8a routing.

    The largest surface the rest of this file does not touch, and the one where
    §21 found two defects of exactly the class this job exists to catch — a
    gateway serving a certificate valid only for `localhost`, and
    `agent_affinity` failing to pin work to the worker holding the stream. Both
    were fixed and neither was guarded by anything.

    An in-cluster agent, enrolled through the real endpoint, dialling the real
    gateway over mTLS.
    """
    section("The agent link, end to end")

    def connected():
        response = ingress.request("GET", "/agents", vhost, token=token)
        if response.status != 200:
            return None
        agents = response.json().get("items") or []
        found = [a for a in agents if a.get("cluster_id") == cluster_id]
        return found[0] if found else None

    agent = wait_for(connected, timeout=180, interval=3.0)
    if not R.check(
        "the enrolled agent is connected",
        agent is not None,
        f"no agent for {cluster_id!r} in GET /agents after 3 minutes. The agent "
        f"dials out, so this is a TLS or address failure rather than a firewall "
        f"one — check the gateway's serving certificate names the Service DNS "
        f"name the manifest tells it to dial (§21 defect 5).",
        ok_detail=f"worker={(agent or {}).get('worker', '?')}",
    ):
        return

    # `declared` means AGENT_GATEWAY_TLS=disabled: the agent asserted its name
    # rather than proving it. A deployment that left that on must not be able
    # to look like one that did not.
    R.check(
        "the agent proved its identity with a certificate",
        agent.get("identity_source") == "certificate",
        f"identity_source is {agent.get('identity_source')!r}, not 'certificate' "
        f"— the agent's cluster id is asserted rather than proved",
        ok_detail=agent.get("identity_source", ""),
    )

    R.check(
        "the agent reports itself online",
        agent.get("online") is not False,
        "the agent registered and is not reporting heartbeats; 'online' is "
        "heartbeat-derived, so this is a live stream that has gone quiet",
    )

    # The M8a assertion, and the reason this whole section exists.
    #
    # **Submitted on the worker that holds the stream, deliberately, because
    # that is the only case the defect breaks.** `AgentPresence.holder()`
    # returns nothing when the record names *this* worker — right for
    # `select_provider`, which reaches it only after the local registry said no
    # — and `agent_affinity` had no such check, so a submission landing on the
    # holder fell through to the *shared* queue where any worker could claim
    # it. Landing on the right worker was precisely the case that un-pinned the
    # job.
    #
    # Submitting through the ingress instead would be nearly useless as a
    # guard. Measured against a rebuilt image with the fix reverted: on four
    # replicas an ingress-routed submission still reached the agent 6 times out
    # of 6, because three quarters of submissions land on a *non*-holder where
    # `holder()` answers correctly. Submitted on the holder, the same mutant
    # failed 3 of 4 with "attached to worker <the worker that accepted it>".
    #
    # Repeated, because even on the holder the mutant succeeds whenever the
    # shared queue happens to hand the job back to the right worker — one in
    # four here. Three rounds put that below 2%.
    holder = (agent.get("worker") or "").split(":")[0]
    if not R.check(
        "the presence record names the worker holding the stream",
        bool(holder),
        f"no worker in the agent record: {agent!r}. Without it this check cannot "
        f"submit where the defect lives, and would be measuring the easy case.",
        ok_detail=holder,
    ):
        return

    outcomes = [_investigate_on(context, namespace, holder, token, cluster_id) for _ in range(3)]
    routed = [o for o in outcomes if o.get("provider") == "agent"]
    R.check(
        "every investigation submitted on the stream holder reaches the agent",
        len(routed) == len(outcomes),
        f"{len(outcomes) - len(routed)} of {len(outcomes)} submissions made on the "
        f"worker holding the stream did not reach the agent: "
        + "; ".join(
            f"status={o.get('status')} provider={o.get('provider')} error={o.get('error', '')[:90]}"
            for o in outcomes
            if o.get("provider") != "agent"
        )
        + ". `agent_affinity` must ask the local registry before `holder()`, or a "
        "submission landing on the holder goes to the shared queue and any "
        "worker may claim it (§21 defect 6).",
        ok_detail=f"{len(routed)}/{len(outcomes)} through the agent",
    )

    final = outcomes[0]
    R.check(
        "the agent-collected investigation succeeded",
        final.get("status") == "succeeded",
        f"status={final.get('status')!r} error={final.get('error')!r}",
    )
    R.check(
        "it collected real evidence through the agent",
        (final.get("usable") or 0) > 0,
        f"no usable evidence: {final!r}",
        ok_detail=f"{final.get('usable')} usable of {final.get('total')} records",
    )

    # Two corroborations, because `cluster_access` is a label the platform
    # writes about itself and this section would otherwise be asserting that
    # label against itself.
    #
    # The first is a *control*: there is no kubeconfig context named after this
    # cluster, so `LocalKubectlProvider` could not have resolved it. A
    # succeeded investigation carrying usable evidence therefore came through
    # the stream — the label is corroborated by the setup rather than trusted.
    contexts = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "k8s-agent",
            "get",
            "secret",
            "k8s-agent-kubeconfig",
            "-o",
            "jsonpath={.data.config}",
        ],
        capture_output=True,
        text=True,
    ).stdout
    local_contexts = base64.b64decode(contexts).decode("utf-8", "replace") if contexts else ""
    R.check(
        "no local kubeconfig context could have answered for this cluster",
        bool(local_contexts) and f"name: {cluster_id}" not in local_contexts,
        f"the mounted kubeconfig has a context named {cluster_id!r} (or could not "
        f"be read at all), so a successful investigation proves nothing about the "
        f"agent — the platform could have answered it locally and labelled it "
        f"`agent`. That is the failure this control exists to exclude.",
        ok_detail="only the agent could have served it",
    )

    # The second is the agent's own account of the connection, which is the one
    # observation on this path that does not come from the platform.
    #
    # It asserts the *connection*, not the collections, and that is a
    # correction: this check first required the agent to have logged about
    # collecting. It does not log that. §21 read collection activity out of
    # client-go's `client-side throttling` warnings — which this milestone's
    # own `--api-qps` fix removed. An assertion inherited from an observation
    # whose cause has since been fixed.
    logs = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "k8s-ops-agent",
            "logs",
            "-l",
            "app=k8s-ops-agent",
            "--tail=200",
        ],
        capture_output=True,
        text=True,
    ).stdout
    R.check(
        "the agent's own logs show it connected over mTLS",
        "connected" in logs and "certificate" in logs and cluster_id in logs,
        f"the agent logged no mTLS connection for {cluster_id!r}. The platform "
        f"says an agent is attached; the process that should have attached has "
        f"no record of it.\n--- agent logs ---\n{logs[-600:]}",
        ok_detail="enrolled and connected",
    )
    return holder


def check_revocation_ends_the_stream(
    context: str, namespace: str, holder: str, token: str, cluster_id: str
) -> None:
    """A revoked certificate must end a *live* stream, not merely fail the next dial.

    This transport is built around a connection that stays open for weeks, so
    revocation-at-reconnect is close to meaningless — `AgentGateway.
    _sweep_revocations()` exists for exactly that reason. It is a background
    task on a timer, which is the shape that goes inert without anything
    noticing: the same family as the correlation-id patcher that was correct,
    called, and produced a constant.

    **The control is the check before this one.** Three investigations already
    reached this agent, so "it no longer serves" means revocation did something.
    Without that pairing an agent that had never worked would pass this
    identically — a chaos scenario with no control, which §18 recorded as the
    way to get a confident number out of nothing.
    """
    section("Revocation ends a live stream")

    revoke = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            namespace,
            "exec",
            holder,
            "--",
            "python",
            "-m",
            "app.agentctl",
            "revoke",
            "--cluster",
            cluster_id,
            "--reason",
            "integration verification",
        ],
        capture_output=True,
        text=True,
    )
    if not R.check(
        "the certificate is revoked",
        revoke.returncode == 0,
        f"agentctl revoke exited {revoke.returncode}: {(revoke.stderr or revoke.stdout)[:300]}",
        ok_detail=revoke.stdout.strip().splitlines()[-1][:70] if revoke.stdout.strip() else "",
    ):
        return

    def no_longer_served():
        outcome = _investigate_on(context, namespace, holder, token, cluster_id)
        served = outcome.get("status") == "succeeded" and outcome.get("provider") == "agent"
        return None if served else outcome

    stopped = wait_for(no_longer_served, timeout=90, interval=5.0)
    R.check(
        "the revoked agent stops serving investigations",
        stopped is not None,
        "the agent kept collecting after its certificate was revoked. The "
        "connect-time check is not enough on a stream that stays open for "
        "weeks — `_sweep_revocations` is what makes revocation take effect on "
        "a live session, and it appears not to be running.",
        ok_detail=f"status={(stopped or {}).get('status')} "
        f"provider={(stopped or {}).get('provider')}",
    )

    # The platform's own account of having done it, which distinguishes "the
    # sweep ended the stream" from "the agent happened to drop off".
    logs = subprocess.run(
        ["kubectl", "--context", context, "-n", namespace, "logs", holder, "--tail=400"],
        capture_output=True,
        text=True,
    ).stdout
    R.check(
        "the gateway logged that it ended the stream",
        "Revoked certificate" in logs,
        "nothing in the holder's log says it ended a revoked stream. The agent "
        "stopped serving, but not demonstrably because of the sweep — which is "
        "the difference between verifying revocation and observing a "
        "coincidence.",
        ok_detail="sweep ended the session",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context", required=True, help="kube context (always pinned, never implicit)"
    )
    parser.add_argument("--namespace", default="k8s-agent")
    parser.add_argument("--release", default="k8s-agent")
    parser.add_argument("--ingress", default="http://127.0.0.1:8080")
    parser.add_argument("--host", default="k8s-agent.local")
    parser.add_argument("--prometheus-host", default="prometheus.local")
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--agent-cluster",
        default="",
        help="cluster id of an enrolled agent; its checks are skipped when absent",
    )
    parser.add_argument(
        "--skip-revocation",
        action="store_true",
        help="leave the agent's certificate valid; revoking it makes a rerun against "
        "the same cluster meaningless, so the local --verify-only loop passes this",
    )
    parser.add_argument("--enrol", default="", help="enrolment mode: mint for this cluster id")
    parser.add_argument("--enrol-out", default="", help="enrolment mode: write the manifest here")
    args = parser.parse_args()

    # Line buffering, because this runs for several minutes and its output is
    # the only thing a CI log shows while it does. Block-buffered stdout means
    # a job that prints nothing until it is over, which is indistinguishable
    # from one that hung.
    sys.stdout.reconfigure(line_buffering=True)

    ingress = Ingress(args.ingress)
    prom = Prometheus(ingress, args.prometheus_host)

    if args.enrol:
        return enrol(ingress, args.host, args.token, args.enrol, args.enrol_out)

    print(f"\033[1mVerifying {args.release} in {args.namespace} on {args.context}\033[0m")

    guarded("agent API versions", check_agent_api_versions, args.context)
    guarded("probes", check_probes, args.context, args.namespace, args.release)
    guarded("ingress", check_ingress, ingress, args.host, args.token)
    guarded("metrics endpoint", check_metrics_endpoint, ingress, args.host)

    scraping = guarded("scrape target", check_scrape_target, prom, args.release) or False
    guarded("alert series", check_alert_series_are_stored, prom)

    baseline = None
    if scraping:
        try:
            baseline = prom.scalar("sum(k8sagent_investigations_total)")
        except Exception as exc:
            print(f"  (baseline unavailable: {exc})")

    _, frames, opened, closed = guarded(
        "investigation", run_investigation, ingress, args.host, args.token
    ) or (None, [], 0.0, 0.0)
    guarded("SSE delivery", check_sse_is_incremental, frames, opened, closed)
    if scraping:
        guarded("counters", check_counters_moved, prom, baseline)

    guarded("autoscaling", check_autoscaling, args.context, args.namespace, args.release)

    if args.agent_cluster:
        holder = guarded(
            "agent link",
            check_agent_path,
            ingress,
            args.host,
            args.token,
            args.context,
            args.namespace,
            args.agent_cluster,
        )
        if holder and not args.skip_revocation:
            guarded(
                "revocation",
                check_revocation_ends_the_stream,
                args.context,
                args.namespace,
                holder,
                args.token,
                args.agent_cluster,
            )
        elif holder:
            print("\n(--skip-revocation: the certificate is left valid)")
    else:
        print("\n(no --agent-cluster given; the agent link is not verified)")

    print("\n" + "=" * 72)
    print(f"{len(R.passed)} passed, {len(R.failed)} failed")
    for name, detail in R.failed:
        print(f"\n\033[31mFAILED\033[0m {name}\n  {detail}")
    print("=" * 72)
    return 1 if R.failed else 0


if __name__ == "__main__":
    sys.exit(main())
