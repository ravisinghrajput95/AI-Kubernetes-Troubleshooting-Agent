"""API-level tests for the synchronous and job-based investigation endpoints."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.auth.dependencies import reset_authenticator
from app.core.config import settings
from app.jobs.runner import InvestigationJobRunner, get_job_runner
from app.jobs.store import InvestigationJobStore, get_job_store
from app.main import app
from tests.test_investigation_service import FakeKubectl


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    """Point every executor at the fake cluster and isolate report output."""
    monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
    monkeypatch.setattr(executor_module.KubectlExecutor, "failing_resources", set(), raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


TOKEN = "test-token"
OTHER_TOKEN = "other-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OTHER_AUTH = {"Authorization": f"Bearer {OTHER_TOKEN}"}


@pytest.fixture
def auth(monkeypatch):
    """Run the API tests against real token authentication.

    Exercising the authenticated path by default means an endpoint that forgets
    its ownership check fails a test rather than shipping.
    """
    monkeypatch.setattr(settings, "auth_mode", "token")
    monkeypatch.setattr(
        settings,
        "api_tokens",
        f"{TOKEN}:alice@example.com:platform,{OTHER_TOKEN}:bob@example.com",
    )
    monkeypatch.setattr(settings, "impersonate_users", False)
    reset_authenticator()
    yield
    reset_authenticator()


@pytest.fixture
def client(cluster, auth):
    store = InvestigationJobStore()
    runner = InvestigationJobRunner(store)
    app.dependency_overrides[get_job_store] = lambda: store
    app.dependency_overrides[get_job_runner] = lambda: runner

    with TestClient(app, headers=AUTH) as test_client:
        test_client.job_store = store
        yield test_client

    app.dependency_overrides.clear()


def wait_for_terminal(client, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/investigations/{job_id}").json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not finish within {timeout}s")


def test_submitting_a_job_returns_202_immediately(client):
    response = client.post("/investigations", json={"context": "test-cluster"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["status_url"] == f"/investigations/{body['id']}"
    assert body["events_url"] == f"/investigations/{body['id']}/events"


def test_job_completes_and_exposes_the_full_result(client):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    body = wait_for_terminal(client, job_id)

    assert body["status"] == "succeeded"
    assert body["diagnosis"]["root_cause"]
    assert body["investigation"]["evidence"]
    assert body["duration_ms"] >= 0


def test_job_id_is_the_report_id(client):
    """The finished report must be addressable under the id returned at submit."""
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    assert client.get(f"/investigations/{job_id}/report").status_code == 200
    assert client.get(f"/investigations/{job_id}/pdf").status_code == 200
    assert client.get(f"/investigations/{job_id}/markdown").status_code == 200

    history = client.get("/investigations").json()["items"]
    assert history[0]["id"] == job_id


def test_progress_events_are_recorded_in_order(client):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    body = wait_for_terminal(client, job_id)

    messages = [event["message"] for event in body["timeline"]]
    assert messages[0] == "Investigation queued"
    assert "Investigation started" in messages
    assert "Retrieved Pods" in messages
    assert "Read Pod Logs" in messages
    assert "Root Cause Generated" in messages
    assert messages[-1] == "Investigation complete"
    assert messages.index("Retrieved Pods") < messages.index("Read Pod Logs")


def test_event_stream_replays_backlog_for_a_finished_job(client):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    with client.stream("GET", f"/investigations/{job_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        body = "".join(response.iter_text())

    assert "event: queued" in body
    assert "event: completed" in body

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert any(item["message"] == "Retrieved Pods" for item in payloads)


def test_active_jobs_are_listed_without_their_payload(client):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    items = client.get("/investigation-jobs").json()["items"]
    assert items[0]["id"] == job_id
    assert "investigation" not in items[0]


def test_collection_failure_fails_the_job_rather_than_the_request(client, monkeypatch):
    def explode(self, args, parse_json=False):
        raise RuntimeError("cluster is on fire")

    monkeypatch.setattr(executor_module.KubectlExecutor, "run", explode)

    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    body = wait_for_terminal(client, job_id)

    assert body["status"] == "failed"
    assert "Verify kubeconfig" in body["error"]


def test_partial_collection_failure_still_succeeds(client, monkeypatch):
    """Losing one inspector degrades the investigation; it does not fail it."""
    monkeypatch.setattr(
        executor_module.KubectlExecutor, "failing_resources", {"pvc"}, raising=False
    )

    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    body = wait_for_terminal(client, job_id)

    assert body["status"] == "succeeded"
    assert 0 < body["investigation"]["evidence_coverage"]["completeness"] < 100
    assert body["diagnosis"]["root_cause"]


def test_a_running_job_can_be_cancelled(client, monkeypatch):
    original = FakeKubectl.run

    def slow(self, args, parse_json=False):
        time.sleep(0.3)
        return original(self, args, parse_json)

    monkeypatch.setattr(executor_module.KubectlExecutor, "run", slow)

    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    time.sleep(0.05)

    assert client.post(f"/investigations/{job_id}/cancel").status_code == 200
    assert wait_for_terminal(client, job_id)["status"] == "cancelled"


def test_unknown_investigation_is_404(client):
    assert client.get("/investigations/does-not-exist").status_code == 404
    assert client.get("/investigations/does-not-exist/events").status_code == 404
    assert client.post("/investigations/does-not-exist/cancel").status_code == 404


def test_cancelling_a_finished_job_conflicts(client):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    assert client.post(f"/investigations/{job_id}/cancel").status_code == 409


def test_status_falls_back_to_the_persisted_report(client):
    """An id must stay addressable after its job leaves the in-memory store."""
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    client.job_store._jobs.clear()

    body = client.get(f"/investigations/{job_id}").json()
    assert body["status"] == "succeeded"
    assert body["persisted"] is True
    assert body["diagnosis"]["root_cause"]


def test_synchronous_endpoint_still_works(client):
    response = client.post("/investigate", json={"context": "test-cluster"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["investigation"]["health"]["status"]
    assert body["diagnosis"]["root_cause"]
    assert body["history_item"]["pdf_url"]


def test_both_paths_produce_the_same_diagnosis(client):
    sync_body = client.post("/investigate", json={"context": "test-cluster"}).json()

    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    async_body = wait_for_terminal(client, job_id)

    assert sync_body["diagnosis"]["root_cause"] == async_body["diagnosis"]["root_cause"]
    assert (
        sync_body["diagnosis"]["selected_hypothesis"]
        == (async_body["diagnosis"]["selected_hypothesis"])
    )


def test_reports_are_written_under_the_isolated_working_directory(client, cluster):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    assert (Path(cluster) / "data" / "investigations" / "reports" / f"{job_id}.pdf").exists()


# --- Surfaces M3 changed ----------------------------------------------------


def test_reports_are_served_with_their_media_type_and_filename(client):
    """The download contract, which stopped being a FileResponse.

    Once a report can be rendered by a different worker there is no local path
    to serve, so the bytes are returned directly. Everything the browser sees
    has to stay the same.
    """
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    expected = {
        "pdf": ("application/pdf", f"investigation-{job_id}.pdf"),
        "json": ("application/json", f"investigation-{job_id}.json"),
        "markdown": ("text/markdown", f"investigation-{job_id}.md"),
    }

    for report_format, (media_type, filename) in expected.items():
        response = client.get(f"/investigations/{job_id}/{report_format}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(media_type)
        assert filename in response.headers["content-disposition"]
        assert response.content

    assert client.get(f"/investigations/{job_id}/pdf").content.startswith(b"%PDF")


def test_a_missing_report_is_404_not_an_error(client):
    assert client.get("/investigations/does-not-exist/pdf").status_code == 404


def test_event_frames_carry_a_sequence_id(client):
    """The SSE id is what lets a reconnecting browser resume."""
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    with client.stream("GET", f"/investigations/{job_id}/events") as response:
        body = "".join(response.iter_text())

    ids = [int(line.removeprefix("id: ")) for line in body.splitlines() if line.startswith("id: ")]
    assert ids, "every frame must carry its sequence"
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))


def test_a_resumed_stream_does_not_replay_what_was_already_seen(client):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    with client.stream("GET", f"/investigations/{job_id}/events") as response:
        first = "".join(response.iter_text())
    ids = [int(line.removeprefix("id: ")) for line in first.splitlines() if line.startswith("id: ")]

    with client.stream(
        "GET",
        f"/investigations/{job_id}/events",
        headers={"Last-Event-ID": str(ids[len(ids) // 2])},
    ) as response:
        resumed = "".join(response.iter_text())

    resumed_ids = [
        int(line.removeprefix("id: ")) for line in resumed.splitlines() if line.startswith("id: ")
    ]
    assert resumed_ids == ids[len(ids) // 2 + 1 :]


def test_a_malformed_resume_header_is_ignored_not_fatal(client):
    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    wait_for_terminal(client, job_id)

    with client.stream(
        "GET",
        f"/investigations/{job_id}/events",
        headers={"Last-Event-ID": "not-a-number"},
    ) as response:
        assert response.status_code == 200
        assert "event: queued" in "".join(response.iter_text())


def test_cancelling_records_the_request_on_the_job(client, monkeypatch):
    """Cancellation is a request, not something the endpoint performs itself."""
    import app.services.investigation_service as service_module

    async def slow_run(self):
        import asyncio

        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(service_module.InvestigationService, "run", slow_run)

    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    time.sleep(0.2)

    assert client.post(f"/investigations/{job_id}/cancel").status_code == 200
    assert client.job_store.get(job_id).cancel_requested is True


def test_a_failed_job_still_reports_what_it_collected(client, monkeypatch):
    """A total collection failure must explain itself, not just name an error.

    The persisted-report fallback already returns `investigation` and
    `diagnosis` for a failed run, so a live job that returned only an error
    made the same id answer with two different shapes depending on whether it
    was still in the store.
    """
    import app.kubernetes.kubectl_executor as executor_module

    monkeypatch.setattr(
        executor_module.KubectlExecutor,
        "failing_resources",
        {
            "pods",
            "events",
            "deployments",
            "nodes",
            "pvc",
            "pv",
            "services",
            "ingress",
            "networkpolicies",
            "endpoints",
            "statefulsets",
            "daemonsets",
            "jobs",
            "cronjobs",
            "namespaces",
            "configmaps",
            "secrets",
        },
        raising=False,
    )

    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    body = wait_for_terminal(client, job_id)

    assert body["status"] == "failed"
    assert body["error"]
    assert body["investigation"]["evidence_coverage"]["usable"] == 0
    assert body["investigation"]["health"]["status"] == "error"
    assert "diagnosis" in body


def test_a_live_failed_job_and_its_persisted_report_agree(client, monkeypatch):
    """The same id must not change shape when the job leaves the store."""
    import app.kubernetes.kubectl_executor as executor_module

    monkeypatch.setattr(
        executor_module.KubectlExecutor,
        "failing_resources",
        {
            "pods",
            "events",
            "deployments",
            "nodes",
            "pvc",
            "pv",
            "services",
            "ingress",
            "networkpolicies",
            "endpoints",
            "statefulsets",
            "daemonsets",
            "jobs",
            "cronjobs",
            "namespaces",
            "configmaps",
            "secrets",
        },
        raising=False,
    )

    job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
    live = wait_for_terminal(client, job_id)

    # Evict the job, so the next read is served from the persisted report.
    client.job_store._jobs.pop(job_id, None)
    persisted = client.get(f"/investigations/{job_id}").json()

    assert persisted["status"] == live["status"] == "failed"
    for key in ("investigation", "diagnosis"):
        assert key in persisted and key in live
    assert (
        persisted["investigation"]["evidence_coverage"]
        == live["investigation"]["evidence_coverage"]
    )


class TestTheStatusEndpointCarriesNoPayload:
    """M8b: the cheap read for callers that only want to know if it is done.

    The polling fallback asks every 1.5 seconds and reads two fields off the
    answer. Served from `/investigations/{id}`, that re-serialised the whole
    finished investigation out of Postgres on every tick — 2.7 MB at the
    `MAX_LIST_ITEMS` ceiling, to render a progress bar. It is already the
    degraded transport because a proxy blocked SSE; it should not also be the
    expensive one.
    """

    def test_it_reports_status_and_timeline(self, client):
        job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
        wait_for_terminal(client, job_id)

        body = client.get(f"/investigations/{job_id}/status").json()

        assert body["status"] == "succeeded"
        assert body["timeline"], "a progress display is mostly the timeline"
        assert body["id"] == job_id

    def test_it_omits_the_investigation_and_the_diagnosis(self, client):
        job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
        wait_for_terminal(client, job_id)

        body = client.get(f"/investigations/{job_id}/status").json()

        assert "investigation" not in body
        assert "diagnosis" not in body

    def test_it_is_much_smaller_than_the_full_read(self, client):
        """The whole point, asserted as bytes rather than as a field list."""
        job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
        wait_for_terminal(client, job_id)

        full = len(client.get(f"/investigations/{job_id}").content)
        status = len(client.get(f"/investigations/{job_id}/status").content)

        assert status * 4 < full, (
            f"the status read is {status} bytes against {full} for the full one; "
            f"it is no longer meaningfully cheaper than the endpoint it exists "
            f"to replace."
        )

    def test_the_full_read_still_carries_everything(self, client):
        """Additive. Changing `/investigations/{id}` would break every consumer
        to benefit one."""
        job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
        body = wait_for_terminal(client, job_id)

        assert body["investigation"]["evidence"]
        assert body["diagnosis"]["root_cause"]

    def test_it_does_not_read_the_payload_from_the_store(self, client):
        """The response being small is not the property; not reading it is.

        Switching this endpoint back to `store.get()` would still produce a
        small response — `to_dict(include_result=False)` drops the payload in
        Python — while the 2.7 MB had already crossed the wire from Postgres,
        which is the whole cost. So this asserts the *call*, not the bytes.
        """
        job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
        wait_for_terminal(client, job_id)

        store = client.job_store
        full_reads: list[str] = []
        original = store.get
        store.get = lambda job: (full_reads.append(job), original(job))[1]
        try:
            assert client.get(f"/investigations/{job_id}/status").status_code == 200
        finally:
            store.get = original

        assert full_reads == [], (
            "the status endpoint performed a full read; the payload left the "
            "database even though the response did not carry it."
        )

    def test_an_unknown_id_is_not_found(self, client):
        assert client.get("/investigations/does-not-exist/status").status_code == 404

    def test_it_answers_for_an_evicted_job_from_the_persisted_report(self, client):
        """An id must not stop being addressable here while still working on
        the full read."""
        job_id = client.post("/investigations", json={"context": "test-cluster"}).json()["id"]
        wait_for_terminal(client, job_id)

        # The fixture injects its own store; evicting from the module global
        # would silently do nothing and leave this asserting the live path.
        client.job_store._jobs.pop(job_id, None)

        body = client.get(f"/investigations/{job_id}/status").json()
        assert body["status"] == "succeeded"
        assert body["persisted"] is True


class TestRequestFieldsAreBounded:
    """Every field on an investigation request names a Kubernetes object, and
    RFC 1123 caps those at 253 characters. Before this they were unbounded: a
    1 MB `context` was accepted with a 202 and written into the job row's
    `request` jsonb *and* into the audit log, which is append-only by design and
    therefore the one store that must stay bounded regardless of what the API
    lets through. See `docs/QA_AUDIT_2026-08-03.md`.
    """

    def test_an_oversized_context_is_refused(self, cluster, auth, client):
        response = client.post("/investigations", json={"context": "x" * 1_000_000}, headers=AUTH)

        assert response.status_code == 422

    def test_it_is_refused_before_any_work_starts(self, cluster, auth, client):
        """422 rather than a 202 followed by a failure — validation that
        happens after the row is written has not bounded the store."""
        response = client.post("/investigations", json={"namespace": "n" * 1_000_000}, headers=AUTH)

        assert response.status_code == 422
        assert "id" not in response.json()

    def test_every_free_text_field_is_bounded(self, cluster, auth, client):
        """One bounded field and three unbounded ones is not a bound."""
        for field in ("context", "namespace", "resource_kind", "resource_name"):
            response = client.post("/investigations", json={field: "z" * 4096}, headers=AUTH)
            assert response.status_code == 422, f"{field} accepted 4096 characters"

    def test_a_realistic_name_is_still_accepted(self, cluster, auth, client):
        """The bound must be generous against real Kubernetes names, or it
        becomes an outage for anyone with long namespaces."""
        response = client.post("/investigations", json={"namespace": "a" * 253}, headers=AUTH)

        assert response.status_code == 202
