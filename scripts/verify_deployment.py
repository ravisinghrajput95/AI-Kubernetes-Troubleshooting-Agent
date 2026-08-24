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
import http.client
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERT_RULES = REPO_ROOT / "deploy" / "alerts" / "k8s-agent-alerts.yaml"

# Below this the alert-series check has lost its subject: the rules file was
# renamed, emptied, or the regex stopped matching, and "all 0 referenced series
# are present" would pass. 15 distinct series are referenced today; the floor
# is 13 so that removing a rule is a reviewed diff rather than a broken check.
MIN_REFERENCED_SERIES = 13

# An SSE stream shorter than this cannot distinguish incremental delivery from
# a buffered blob, so the check refuses to conclude rather than passing.
MIN_STREAM_SECONDS = 0.75
# Spread between the first and last frame's *arrival*. Under nginx's default
# proxy_buffering this is ~0: every frame lands at once when the response ends.
MIN_FRAME_SPREAD_SECONDS = 0.30


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
                headers={"Host": vhost, "Accept": "text/event-stream", "Authorization": f"Bearer {token}"},
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
            raise RuntimeError(f"Prometheus {path} -> HTTP {response.status}: {response.text[:200]}")
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
    if R.check("GET /health/ready", ready.status == 200, f"HTTP {ready.status}: {ready.text[:200]}"):
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

    unhealthy = [(t["scrapeUrl"], t.get("health"), t.get("lastError", "")) for t in targets if t.get("health") != "up"]
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
    if wait_for(lambda: not absent(), timeout=90) is None:
        missing = absent()
    else:
        missing = []

    R.check(
        "every referenced series is present in Prometheus",
        not missing,
        f"{len(missing)} of {len(names)} series the alert rules depend on are "
        f"not in Prometheus: {missing}. Such a rule evaluates successfully and "
        f"fires never.",
        ok_detail=f"{len(names)}/{len(names)} present",
    )


def run_investigation(ingress: Ingress, vhost: str, token: str) -> tuple[str | None, list, float, float]:
    """Submit an investigation through the ingress and stream its progress."""
    section("An investigation runs end to end through the ingress")

    accepted = ingress.request(
        "POST", "/investigations", vhost, token=token, body={"namespace": "kube-system"}
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
        return state.json() if state.json().get("status") in {"succeeded", "failed", "cancelled"} else None

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
    """SSE frames must arrive as they are produced, not as one blob at the end.

    nginx buffers proxied responses by default; `X-Accel-Buffering: no`, set in
    `app/api/investigate.py`, is what turns that off per response. Nothing in
    the repository pins it, and losing it is invisible from inside the pod — the
    application streams correctly either way, and the console's live timeline
    simply arrives all at once at the end of an incident.
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

    duration = closed - opened
    spread = frames[-1][0] - frames[0][0]

    # The §18 lesson, applied: the drain scenario reported PASS while the
    # process exited 0.2s after SIGTERM with nothing in flight. An investigation
    # that finished in 40ms would show a ~0 spread whether nginx buffered or
    # not, so refuse to conclude rather than passing.
    if not R.check(
        "the stream lasted long enough for the question to mean anything",
        duration >= MIN_STREAM_SECONDS,
        f"the whole stream took {duration:.3f}s (floor {MIN_STREAM_SECONDS}s). "
        f"Buffered and unbuffered delivery are indistinguishable over that "
        f"window, so this check is refusing to report a pass it did not earn.",
        ok_detail=f"{duration:.2f}s",
    ):
        return

    R.check(
        "frame arrivals are spread across the stream",
        spread >= MIN_FRAME_SPREAD_SECONDS,
        f"all {len(frames)} frames arrived within {spread * 1000:.0f}ms of each "
        f"other over a {duration:.2f}s stream — that is a buffered blob. nginx "
        f"buffers proxied responses unless the upstream sends "
        f"`X-Accel-Buffering: no` (app/api/investigate.py SSE_HEADERS).",
        ok_detail=f"{spread:.2f}s spread over {duration:.2f}s, "
        f"first frame at +{frames[0][0] - opened:.2f}s",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="kube context (always pinned, never implicit)")
    parser.add_argument("--namespace", default="k8s-agent")
    parser.add_argument("--release", default="k8s-agent")
    parser.add_argument("--ingress", default="http://127.0.0.1:8080")
    parser.add_argument("--host", default="k8s-agent.local")
    parser.add_argument("--prometheus-host", default="prometheus.local")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    # Line buffering, because this runs for several minutes and its output is
    # the only thing a CI log shows while it does. Block-buffered stdout means
    # a job that prints nothing until it is over, which is indistinguishable
    # from one that hung.
    sys.stdout.reconfigure(line_buffering=True)

    ingress = Ingress(args.ingress)
    prom = Prometheus(ingress, args.prometheus_host)

    print(f"\033[1mVerifying {args.release} in {args.namespace} on {args.context}\033[0m")

    check_probes(args.context, args.namespace, args.release)
    check_ingress(ingress, args.host, args.token)
    check_metrics_endpoint(ingress, args.host)

    scraping = check_scrape_target(prom, args.release)
    check_alert_series_are_stored(prom)

    baseline = None
    if scraping:
        try:
            baseline = prom.scalar("sum(k8sagent_investigations_total)")
        except Exception as exc:
            print(f"  (baseline unavailable: {exc})")

    _, frames, opened, closed = run_investigation(ingress, args.host, args.token)
    check_sse_is_incremental(frames, opened, closed)
    if scraping:
        check_counters_moved(prom, baseline)

    check_autoscaling(args.context, args.namespace, args.release)

    print("\n" + "=" * 72)
    print(f"{len(R.passed)} passed, {len(R.failed)} failed")
    for name, detail in R.failed:
        print(f"\n\033[31mFAILED\033[0m {name}\n  {detail}")
    print("=" * 72)
    return 1 if R.failed else 0


if __name__ == "__main__":
    sys.exit(main())
