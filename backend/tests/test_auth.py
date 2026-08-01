"""Authentication, impersonation, and ownership.

F13 from the 2026-07-26 review: every endpoint was unauthenticated while the
service held a kubeconfig, making it an open cluster-read proxy with a report
archive attached.

Authentication alone is not sufficient — it decides *whether* you get in, not
*what you can see*. Impersonation is what makes the cluster apply the caller's
own RBAC.
"""

import time

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.auth.authenticators import (
    DisabledAuthenticator,
    OIDCAuthenticator,
    StaticTokenAuthenticator,
    build_authenticator,
)
from app.auth.dependencies import reset_authenticator
from app.auth.models import ANONYMOUS, AuthenticationError, Principal
from app.core.config import settings
from app.kubernetes.kubectl_executor import KubectlExecutor
from app.main import app
from tests.test_investigation_service import FakeKubectl

ALICE = Principal(subject="alice@example.com", groups=("platform",), auth_method="token")


class TestStartupGuard:
    def test_disabled_auth_is_refused_unless_acknowledged(self, monkeypatch):
        """Failing open is the failure mode that matters; it must be deliberate."""
        monkeypatch.setattr(settings, "auth_mode", "disabled")
        monkeypatch.setattr(settings, "allow_insecure_no_auth", False)

        with pytest.raises(ValueError, match="ALLOW_INSECURE_NO_AUTH"):
            build_authenticator()

    def test_disabled_auth_is_allowed_when_acknowledged(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "disabled")
        monkeypatch.setattr(settings, "allow_insecure_no_auth", True)

        assert isinstance(build_authenticator(), DisabledAuthenticator)

    def test_unknown_mode_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "magic")
        with pytest.raises(ValueError, match="Unknown AUTH_MODE"):
            build_authenticator()

    def test_token_mode_requires_tokens(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "api_tokens", "")
        with pytest.raises(ValueError, match="API_TOKENS"):
            build_authenticator()

    def test_oidc_mode_requires_issuer_and_audience(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "oidc")
        monkeypatch.setattr(settings, "oidc_issuer", "")
        with pytest.raises(ValueError, match="OIDC_ISSUER"):
            build_authenticator()


class TestStaticTokens:
    def test_valid_token_yields_its_identity(self):
        auth = StaticTokenAuthenticator.from_config("tok1:alice@example.com:platform|sre")
        principal = auth.authenticate("tok1")

        assert principal.subject == "alice@example.com"
        assert principal.groups == ("platform", "sre")
        assert principal.auth_method == "token"

    @pytest.mark.parametrize("credential", [None, "", "wrong", "tok", "tok1 "])
    def test_invalid_credentials_are_rejected(self, credential):
        auth = StaticTokenAuthenticator.from_config("tok1:alice@example.com")
        with pytest.raises(AuthenticationError):
            auth.authenticate(credential)

    def test_malformed_configuration_is_rejected(self):
        for raw in ["justatoken", ":nosubject", "tok:"]:
            with pytest.raises(ValueError):
                StaticTokenAuthenticator.from_config(raw)

    def test_multiple_tokens_map_to_distinct_identities(self):
        auth = StaticTokenAuthenticator.from_config("a:alice@x.com,b:bob@x.com")
        assert auth.authenticate("a").subject == "alice@x.com"
        assert auth.authenticate("b").subject == "bob@x.com"


class TestOIDC:
    def test_expired_token_is_rejected(self, monkeypatch):
        import jwt

        auth = OIDCAuthenticator.__new__(OIDCAuthenticator)
        auth.issuer, auth.audience = "https://idp", "k8s-agent"
        auth.username_claim, auth.groups_claim = "email", "groups"

        class Keys:
            def get_signing_key_from_jwt(self, token):
                raise jwt.ExpiredSignatureError("expired")

        auth._jwks = Keys()
        with pytest.raises(AuthenticationError, match="expired"):
            auth.authenticate("token")

    def test_invalid_signature_is_rejected(self):
        import jwt

        auth = OIDCAuthenticator.__new__(OIDCAuthenticator)
        auth.issuer, auth.audience = "https://idp", "k8s-agent"
        auth.username_claim, auth.groups_claim = "email", "groups"

        class Keys:
            def get_signing_key_from_jwt(self, token):
                raise jwt.InvalidSignatureError("bad signature")

        auth._jwks = Keys()
        with pytest.raises(AuthenticationError, match="Invalid token"):
            auth.authenticate("token")

    def test_missing_credential_is_rejected(self):
        auth = OIDCAuthenticator.__new__(OIDCAuthenticator)
        with pytest.raises(AuthenticationError, match="Missing"):
            auth.authenticate(None)


class TestImpersonation:
    def test_cluster_reads_run_as_the_calling_user(self, monkeypatch):
        monkeypatch.setattr(settings, "impersonate_users", True)
        executor = KubectlExecutor(context="prod", principal=ALICE)

        args = executor._impersonation_args(["get", "pods", "-A"])
        assert args == ["--as", "alice@example.com", "--as-group", "platform"]

    def test_impersonation_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "impersonate_users", False)
        executor = KubectlExecutor(context="prod", principal=ALICE)
        assert executor._impersonation_args(["get", "pods"]) == []

    def test_anonymous_callers_are_not_impersonated(self, monkeypatch):
        monkeypatch.setattr(settings, "impersonate_users", True)
        executor = KubectlExecutor(principal=ANONYMOUS)
        assert executor._impersonation_args(["get", "pods"]) == []

    def test_local_kubeconfig_reads_are_not_impersonated(self, monkeypatch):
        """There is no API server call to impersonate against."""
        monkeypatch.setattr(settings, "impersonate_users", True)
        executor = KubectlExecutor(principal=ALICE)
        assert executor._impersonation_args(["config", "get-contexts"]) == []

    def test_impersonation_flags_reach_the_command(self, monkeypatch):
        monkeypatch.setattr(settings, "impersonate_users", True)

        class Recording(FakeKubectl):
            def __init__(self):
                super().__init__()
                self.principal = ALICE

        executor = Recording()
        # FakeKubectl overrides run(); assert on the real builder instead.
        real = KubectlExecutor(context="prod", principal=ALICE)
        assert "--as" in real._impersonation_args(["get", "pods"])
        assert executor.principal.subject == "alice@example.com"


TOKENS = "alice-tok:alice@example.com,bob-tok:bob@example.com"


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
    monkeypatch.setattr(executor_module.KubectlExecutor, "failing_resources", set(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "auth_mode", "token")
    monkeypatch.setattr(settings, "api_tokens", TOKENS)
    monkeypatch.setattr(settings, "impersonate_users", False)
    reset_authenticator()

    with TestClient(app) as client:
        yield client

    reset_authenticator()
    app.dependency_overrides.clear()


ALICE_AUTH = {"Authorization": "Bearer alice-tok"}
BOB_AUTH = {"Authorization": "Bearer bob-tok"}

PROTECTED = [
    ("get", "/clusters"),
    ("get", "/investigations"),
    ("get", "/investigation-jobs"),
    ("get", "/investigations/abc"),
    ("get", "/investigations/abc/report"),
    ("get", "/investigations/abc/pdf"),
    ("post", "/investigate"),
    ("post", "/investigations"),
    ("post", "/investigations/abc/cancel"),
    ("post", "/investigations/abc/regenerate"),
]


class TestEndpointProtection:
    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_every_endpoint_rejects_anonymous_requests(self, api, method, path):
        response = getattr(api, method)(path)

        assert response.status_code == 401, f"{method.upper()} {path} was not protected"
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_every_endpoint_rejects_an_invalid_token(self, api, method, path):
        response = getattr(api, method)(path, headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_health_stays_unauthenticated_for_probes(self, api):
        assert api.get("/health").status_code == 200


class TestOwnership:
    def _run(self, api, headers, timeout: float = 30.0):
        """Submit an investigation and wait for it to reach a terminal state.

        A wall-clock deadline with a real sleep, not a fixed iteration count.
        The previous version polled 200 times as fast as it could and called
        that a timeout, which made it a bet on how many requests fit in the
        time an investigation takes — a bet it lost on the slower of the two
        CI Pythons once report rendering got a little heavier. Time is the
        thing being waited on, so time is what the loop should measure.
        """
        submitted = api.post("/investigations", json={"context": "test"}, headers=headers)
        job_id = submitted.json()["id"]

        deadline = time.monotonic() + timeout
        state: dict = {}
        while time.monotonic() < deadline:
            state = api.get(f"/investigations/{job_id}", headers=headers).json()
            if state["status"] in {"succeeded", "failed", "cancelled"}:
                return job_id, state
            # Yields to the portal thread running the app, rather than
            # starving it by re-entering immediately.
            time.sleep(0.02)

        raise AssertionError(
            f"investigation {job_id} did not finish within {timeout}s; "
            f"last status was {state.get('status', 'unknown')!r}"
        )

    def test_history_only_lists_your_own_investigations(self, api):
        self._run(api, ALICE_AUTH)

        assert len(api.get("/investigations", headers=ALICE_AUTH).json()["items"]) == 1
        assert api.get("/investigations", headers=BOB_AUTH).json()["items"] == []

    def test_another_user_cannot_read_your_report(self, api):
        job_id, _ = self._run(api, ALICE_AUTH)

        assert api.get(f"/investigations/{job_id}/report", headers=ALICE_AUTH).status_code == 200
        assert api.get(f"/investigations/{job_id}/report", headers=BOB_AUTH).status_code == 404

    def test_another_user_cannot_download_your_pdf(self, api):
        job_id, _ = self._run(api, ALICE_AUTH)

        assert api.get(f"/investigations/{job_id}/pdf", headers=ALICE_AUTH).status_code == 200
        assert api.get(f"/investigations/{job_id}/pdf", headers=BOB_AUTH).status_code == 404

    def test_another_user_cannot_read_job_state(self, api):
        job_id, _ = self._run(api, ALICE_AUTH)
        assert api.get(f"/investigations/{job_id}", headers=BOB_AUTH).status_code == 404

    def test_another_user_cannot_regenerate_your_report(self, api):
        job_id, _ = self._run(api, ALICE_AUTH)
        assert api.post(f"/investigations/{job_id}/regenerate", headers=BOB_AUTH).status_code == 404

    def test_denial_looks_like_absence(self, api):
        """403 would confirm the id exists; 404 does not."""
        job_id, _ = self._run(api, ALICE_AUTH)

        denied = api.get(f"/investigations/{job_id}/report", headers=BOB_AUTH)
        missing = api.get(
            "/investigations/00000000-0000-0000-0000-000000000000/report", headers=BOB_AUTH
        )
        assert denied.status_code == missing.status_code == 404


class TestHealthReportsTheAuthMode:
    """The console renders the sign-in the backend actually requires.

    Without this it cannot tell a token deployment from an OIDC one, and cannot
    warn that a deployment is accepting unauthenticated requests — which is
    exactly the configuration that should be hardest to run by accident.
    """

    def test_disabled_mode_is_reported_as_insecure(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "disabled")
        with TestClient(app) as client:
            body = client.get("/health").json()

        assert body["auth_mode"] == "disabled"
        assert body["insecure"] is True

    def test_token_mode_is_not_insecure(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "token")
        with TestClient(app) as client:
            body = client.get("/health").json()

        assert body["auth_mode"] == "token"
        assert body["insecure"] is False

    def test_health_stays_unauthenticated(self, monkeypatch):
        """Otherwise the console could never learn how to authenticate."""
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "api_tokens", "secret:alice@example.com")
        reset_authenticator()
        try:
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200
        finally:
            reset_authenticator()

    def test_no_token_material_is_exposed(self, monkeypatch):
        """A liveness probe must not become a credential oracle."""
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "api_tokens", "supersecret:alice@example.com")
        with TestClient(app) as client:
            raw = client.get("/health").text

        assert "supersecret" not in raw
        assert "alice@example.com" not in raw
