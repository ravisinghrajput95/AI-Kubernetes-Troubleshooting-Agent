"""M6's exit criterion: two tenants, provably isolated.

The word doing the work is *provably*. It would be easy to write tests that
call `store.list(owner=...)` and confirm the filter filters — and prove
nothing, because the failure this milestone exists to prevent is the query
somebody adds next year without the filter.

So the isolation tests here deliberately **do not go through the store**. They
open a raw cursor and run `SELECT * FROM investigations` with no WHERE clause
at all, which is the worst thing a future contributor could write, and assert
it returns only the caller's rows. If that holds, the isolation is a property
of the schema rather than of everyone's memory.

The Postgres half is opt-in behind `K8S_AGENT_INTEGRATION=1`, for the same
reason as every other database test: row-level security has no in-memory
equivalent, and a fake would prove the fake works.
"""

import pytest

from app.auth.models import Principal
from app.authz.models import Role
from app.security.identity import identity_from_pem
from app.tenancy import (
    DEFAULT_TENANT,
    SYSTEM_TENANT,
    Tenant,
    TenantError,
    current_tenant,
    is_system_scope,
    system_scope,
    tenant_scope,
    valid_tenant_id,
)
from tests.distributed_backend import INTEGRATION_ENABLED, SKIP_REASON, DistributedBackend

ACME = "acme"
GLOBEX = "globex"


class TestTheAmbientTenant:
    """The tenant is context, not an argument. That is the whole design."""

    def test_the_default_is_the_single_tenant(self):
        assert current_tenant() == DEFAULT_TENANT

    def test_a_scope_applies_and_unwinds(self):
        with tenant_scope(ACME):
            assert current_tenant() == ACME
        assert current_tenant() == DEFAULT_TENANT

    def test_scopes_nest(self):
        with tenant_scope(ACME):
            with tenant_scope(GLOBEX):
                assert current_tenant() == GLOBEX
            assert current_tenant() == ACME

    def test_an_exception_still_unwinds_the_scope(self):
        """A leaked scope would run the next request as the previous tenant."""
        with pytest.raises(RuntimeError), tenant_scope(ACME):
            raise RuntimeError("boom")
        assert current_tenant() == DEFAULT_TENANT

    async def test_a_task_inherits_the_tenant_that_created_it(self):
        """The reason this is a ContextVar and not a module global.

        A background investigation is started from a request and outlives it.
        It must keep the tenant that submitted it, and must not be affected by
        whatever the worker does next.
        """
        import asyncio

        seen: list[str] = []

        async def background() -> None:
            await asyncio.sleep(0.01)
            seen.append(current_tenant())

        with tenant_scope(ACME):
            task = asyncio.create_task(background())

        # The scope has exited here, and a global would now read `default`.
        with tenant_scope(GLOBEX):
            await task

        assert seen == [ACME]

    @pytest.mark.parametrize(
        "value", ["", "Acme", "acme corp", "acme/globex", "-acme", "a" * 64, SYSTEM_TENANT]
    )
    def test_an_unusable_tenant_id_is_refused(self, value):
        assert not valid_tenant_id(value)
        with pytest.raises(TenantError), tenant_scope(value):
            pass

    def test_the_system_marker_cannot_be_a_tenant(self):
        """`*` is how the policies spell "every tenant".

        If it were a valid tenant id, a token claim of `*` would authenticate
        somebody into seeing everything.
        """
        assert not valid_tenant_id(SYSTEM_TENANT)
        with pytest.raises(TenantError):
            Tenant(id=SYSTEM_TENANT)

    def test_system_scope_is_recognisable(self):
        assert not is_system_scope()
        with system_scope():
            assert is_system_scope()
            assert current_tenant() == SYSTEM_TENANT
        assert not is_system_scope()


class TestTheSystemEscapeIsNarrow:
    def test_only_the_queue_consumer_uses_it(self):
        """A deliberate hole stays deliberate only if it stays small.

        `system_scope()` disables tenant isolation for the block it wraps. The
        two legitimate callers are the queue consumer's claim and the reaper,
        neither of which can know a tenant before reading the row that names
        one. Anything else appearing here is a bug this test is meant to catch.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app"
        users = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "system_scope(" in path.read_text(encoding="utf-8")
            and path.name not in {"context.py", "__init__.py"}
        }

        assert users == {"jobs/consumer.py"}, (
            f"system_scope() escapes tenant isolation and is now used by {users}. "
            f"If a new caller is genuinely infrastructure, add it here deliberately."
        )


class TestPrincipalsCarryATenant:
    def test_a_principal_round_trips_its_tenant(self):
        """A worker that did not receive the request rebuilds the caller."""
        original = Principal(subject="alice", tenant=ACME)
        assert Principal.from_dict(original.to_dict()).tenant == ACME

    def test_a_principal_stored_before_m6_reads_as_the_default_tenant(self):
        rebuilt = Principal.from_dict({"subject": "alice", "groups": []})
        assert rebuilt.tenant == DEFAULT_TENANT

    def test_token_configuration_carries_a_tenant(self):
        from app.auth.authenticators import StaticTokenAuthenticator

        authenticator = StaticTokenAuthenticator.from_config("tok-a:alice:sre:acme,tok-b:bob:sre")

        assert authenticator.authenticate("tok-a").tenant == ACME
        # Omitted, so the single tenant — every pre-M6 configuration.
        assert authenticator.authenticate("tok-b").tenant == DEFAULT_TENANT

    def test_an_unusable_tenant_in_configuration_is_refused_at_startup(self):
        from app.auth.authenticators import StaticTokenAuthenticator

        with pytest.raises(TenantError):
            StaticTokenAuthenticator.from_config("tok:alice:sre:Not A Tenant")


class TestAgentIdentityCarriesATenant:
    def test_an_issued_certificate_names_its_tenant(self):
        from app.security.ca import CertificateAuthority
        from tests.test_agent_identity import make_csr

        authority = CertificateAuthority.create("test.local")
        csr, _ = make_csr()
        issued = authority.issue_from_csr(csr, "prod", tenant=ACME)

        identity = identity_from_pem(issued.certificate_pem, "test.local")
        assert identity.tenant == ACME
        assert identity.cluster_id == "prod"

    def test_two_tenants_may_name_a_cluster_the_same(self):
        from app.security.ca import CertificateAuthority
        from tests.test_agent_identity import make_csr

        authority = CertificateAuthority.create("test.local")
        first = authority.issue_from_csr(make_csr()[0], "prod", tenant=ACME)
        second = authority.issue_from_csr(make_csr()[0], "prod", tenant=GLOBEX)

        assert identity_from_pem(first.certificate_pem, "test.local").tenant == ACME
        assert identity_from_pem(second.certificate_pem, "test.local").tenant == GLOBEX


class TestConfigurationRefusesHalfMeasures:
    def test_shared_tenancy_without_postgres_is_refused(self, monkeypatch):
        """There is no in-memory row-level security to fall back to."""
        from app.core.config import Settings

        config = Settings(TENANCY_MODE="shared", DATABASE_URL="", AUTH_MODE="token")
        with pytest.raises(RuntimeError, match="requires DATABASE_URL"):
            config.validate_tenancy()

    def test_shared_tenancy_without_authentication_is_refused(self):
        """Every caller anonymous means every caller the same tenant."""
        from app.core.config import Settings

        config = Settings(
            TENANCY_MODE="shared",
            DATABASE_URL="postgresql://localhost/x",
            AUTH_MODE="disabled",
        )
        with pytest.raises(RuntimeError, match="requires authentication"):
            config.validate_tenancy()

    def test_an_unknown_mode_is_refused_rather_than_defaulted(self):
        from app.core.config import Settings

        with pytest.raises(RuntimeError, match="not a mode"):
            Settings(TENANCY_MODE="multi").validate_tenancy()

    def test_single_tenancy_needs_nothing(self):
        from app.core.config import Settings

        Settings(TENANCY_MODE="single").validate_tenancy()


# --- the exit criterion -----------------------------------------------------


class Unprivileged:
    """A backend whose database connection cannot bypass row-level security."""

    def __init__(self, backend: DistributedBackend) -> None:
        self._backend = backend
        self.database = backend.unprivileged()

    def enrolment(self):
        from app.persistence.agent_identity import PostgresEnrolmentStore

        return PostgresEnrolmentStore(self.database)

    def close(self) -> None:
        self.database.close()


@pytest.fixture
async def backend():
    """Postgres, connected as an application role rather than a superuser.

    This distinction is the test, not a detail of it. Run as `postgres` every
    assertion below passes with row-level security doing nothing at all —
    which is how the first version of this suite went green against a
    deployment that had no isolation whatsoever.
    """
    if not INTEGRATION_ENABLED:
        pytest.skip(SKIP_REASON)

    instance = DistributedBackend(with_bus=False)
    limited = Unprivileged(instance)
    try:
        yield limited
    finally:
        limited.close()
        await instance.close()


def rows_visible(backend, table: str) -> list[tuple]:
    """A query with no WHERE clause. The worst thing a contributor could write."""
    with backend.database.cursor() as cursor:
        cursor.execute(f"SELECT id FROM {table}")
        return cursor.fetchall()


class TestTwoTenantsAreIsolated:
    """The milestone's exit criterion, asserted against real row-level security."""

    def seed(self, backend) -> None:
        with tenant_scope(ACME), backend.database.cursor() as cursor:
            cursor.execute(
                "INSERT INTO investigations (id, owner, status) VALUES (%s, %s, %s)",
                ("acme-1", "alice", "succeeded"),
            )
        with tenant_scope(GLOBEX), backend.database.cursor() as cursor:
            cursor.execute(
                "INSERT INTO investigations (id, owner, status) VALUES (%s, %s, %s)",
                ("globex-1", "bob", "succeeded"),
            )

    async def test_an_unfiltered_select_returns_only_one_tenants_rows(self, backend):
        self.seed(backend)

        with tenant_scope(ACME):
            assert [row[0] for row in rows_visible(backend, "investigations")] == ["acme-1"]

        with tenant_scope(GLOBEX):
            assert [row[0] for row in rows_visible(backend, "investigations")] == ["globex-1"]

    async def test_the_tenant_is_stamped_without_anyone_passing_it(self, backend):
        """No store method mentions `tenant_id`. The column default does."""
        self.seed(backend)

        with system_scope(), backend.database.cursor() as cursor:
            cursor.execute("SELECT id, tenant_id FROM investigations ORDER BY id")
            assert dict(cursor.fetchall()) == {"acme-1": ACME, "globex-1": GLOBEX}

    async def test_one_tenant_cannot_read_anothers_row_by_id(self, backend):
        """Knowing the id is not access. Guessing one must not be either."""
        self.seed(backend)

        with tenant_scope(GLOBEX), backend.database.cursor() as cursor:
            cursor.execute("SELECT id FROM investigations WHERE id = %s", ("acme-1",))
            assert cursor.fetchone() is None

    async def test_one_tenant_cannot_update_anothers_row(self, backend):
        self.seed(backend)

        with tenant_scope(GLOBEX), backend.database.cursor() as cursor:
            cursor.execute("UPDATE investigations SET status = 'failed' WHERE id = 'acme-1'")
            assert cursor.rowcount == 0

        with tenant_scope(ACME), backend.database.cursor() as cursor:
            cursor.execute("SELECT status FROM investigations WHERE id = 'acme-1'")
            assert cursor.fetchone()[0] == "succeeded"

    async def test_one_tenant_cannot_delete_anothers_row(self, backend):
        self.seed(backend)

        with tenant_scope(GLOBEX), backend.database.cursor() as cursor:
            cursor.execute("DELETE FROM investigations WHERE id = 'acme-1'")
            assert cursor.rowcount == 0

    async def test_a_row_cannot_be_written_into_another_tenant(self, backend):
        """WITH CHECK, not just USING. Reading is half the control."""
        import psycopg

        with (
            pytest.raises(psycopg.errors.Error),
            tenant_scope(GLOBEX),
            backend.database.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO investigations (id, owner, status, tenant_id) VALUES (%s, %s, %s, %s)",
                ("smuggled", "bob", "succeeded", ACME),
            )

    async def test_the_job_store_isolates_without_knowing_it_does(self, backend):
        """The store has no tenant parameter, and never gained one."""
        from app.jobs.store import InMemoryJobStore  # noqa: F401  (contrast only)

        store = backend.enrolment()  # any store on the same database

        with tenant_scope(ACME):
            store.issue_token("prod")
        with tenant_scope(GLOBEX):
            assert store.tokens() == []
            assert store.certificates() == []

        with tenant_scope(ACME):
            assert len(store.tokens()) == 1

    async def test_a_bootstrap_token_cannot_be_spent_by_another_tenant(self, backend):
        """Otherwise a leaked token enrols a cluster into the wrong fleet."""
        store = backend.enrolment()

        with tenant_scope(ACME):
            token = store.issue_token("prod")

        with tenant_scope(GLOBEX):
            assert store.spend_token(token) is None

        with tenant_scope(ACME):
            assert store.spend_token(token) == "prod"

    async def test_the_system_scope_sees_everything(self, backend):
        """Which is why it is one function and not an argument."""
        self.seed(backend)

        with system_scope():
            assert sorted(row[0] for row in rows_visible(backend, "investigations")) == [
                "acme-1",
                "globex-1",
            ]

    async def test_an_unset_tenant_sees_nothing(self, backend):
        """A connection used outside any scope is closed, not open.

        The default for a security control has to be "no rows", or a code path
        that forgets to set a tenant becomes a code path that reads everyone's.
        """
        self.seed(backend)

        with backend.database.cursor() as cursor:
            cursor.execute("SELECT set_config('app.current_tenant', '', true)")
            cursor.execute("SELECT id FROM investigations")
            assert cursor.fetchall() == []


class TestRolesAreIsolatedBetweenTenants:
    """Migration 004 under the same policy as 003.

    A membership row decides what somebody may *do*, so a leak here is not a
    disclosure — it is a privilege grant. These assertions are deliberately the
    same shape as the ones above, including the unfiltered SELECT: the point is
    that the new table inherited the property rather than reimplemented it.
    """

    def members(self, backend):
        from app.persistence.members import PostgresMemberStore

        return PostgresMemberStore(backend.database)

    async def test_a_member_of_one_tenant_is_invisible_in_another(self, backend):
        store = self.members(backend)

        with tenant_scope(ACME):
            store.upsert("alice@acme.com", Role.OWNER)
        with tenant_scope(GLOBEX):
            assert store.get("alice@acme.com") is None
            assert store.list() == []
            assert store.count_owners() == 0

    async def test_an_unfiltered_select_returns_only_one_tenants_members(self, backend):
        store = self.members(backend)

        with tenant_scope(ACME):
            store.upsert("alice@acme.com", Role.OWNER)
        with tenant_scope(GLOBEX):
            store.upsert("bob@globex.com", Role.VIEWER)

        with tenant_scope(ACME), backend.database.cursor() as cursor:
            cursor.execute("SELECT subject FROM tenant_members")
            assert [row[0] for row in cursor.fetchall()] == ["alice@acme.com"]

    async def test_the_same_person_may_hold_different_roles_in_two_tenants(self, backend):
        """A consultant with two customers is one subject and two memberships."""
        store = self.members(backend)

        with tenant_scope(ACME):
            store.upsert("consultant@example.com", Role.OWNER)
        with tenant_scope(GLOBEX):
            store.upsert("consultant@example.com", Role.VIEWER)

        with tenant_scope(ACME):
            assert store.get("consultant@example.com").role is Role.OWNER
        with tenant_scope(GLOBEX):
            assert store.get("consultant@example.com").role is Role.VIEWER

    async def test_one_tenant_cannot_promote_itself_inside_another(self, backend):
        """WITH CHECK, and the reason it matters more here than anywhere else.

        Writing a row into another tenant's investigations would leak data.
        Writing one into another tenant's `tenant_members` would make you an
        owner of their organisation.
        """
        import psycopg

        with (
            pytest.raises(psycopg.errors.Error),
            tenant_scope(GLOBEX),
            backend.database.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO tenant_members (subject, role, tenant_id) VALUES (%s, %s, %s)",
                ("attacker@globex.com", "owner", ACME),
            )

    async def test_one_tenant_cannot_promote_an_existing_member_of_another(self, backend):
        store = self.members(backend)
        with tenant_scope(ACME):
            store.upsert("alice@acme.com", Role.VIEWER)

        with tenant_scope(GLOBEX), backend.database.cursor() as cursor:
            cursor.execute(
                "UPDATE tenant_members SET role = 'owner' WHERE subject = 'alice@acme.com'"
            )
            assert cursor.rowcount == 0

        with tenant_scope(ACME):
            assert store.get("alice@acme.com").role is Role.VIEWER


class TestTheTenantSurvivesTheDependency:
    """The ambient tenant has to reach the *database*, not just the dependency.

    This suite proved isolation by entering `tenant_scope()` by hand, which
    proves the schema works and says nothing about whether a real request ever
    gets there. It did not: `require_principal` was a synchronous dependency,
    FastAPI runs those in a worker thread, and a worker thread receives a
    *copy* of the context — so `_current.set(principal.tenant)` applied to a
    context that was thrown away when the dependency returned.

    Every request therefore ran as `default` regardless of who called. Rows
    from every tenant were written into one, and every tenant could read every
    other tenant's, with the policies enabled, forced and correct. It is the
    same failure M6 already caught once at the database role, one layer up:
    a control that is present, correct, and inert.

    The assertion is on the value the request *would hand to Postgres*, because
    that is the thing that was wrong. Asserting on `principal.tenant` passes
    with the bug present.
    """

    def _client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        import app.kubernetes.kubectl_executor as executor_module
        from app.auth.dependencies import reset_authenticator
        from app.core.config import settings
        from app.main import app
        from tests.test_investigation_service import FakeKubectl

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "api_tokens", f"acme-tok:alice@acme.com::{ACME}")
        monkeypatch.setattr(settings, "impersonate_users", False)
        reset_authenticator()
        return TestClient(app)

    def test_the_tenant_reaches_the_layer_that_talks_to_postgres(self, monkeypatch, tmp_path):
        """The direct form: what `Database.cursor()` would announce.

        `app.authz.store` reads `current_tenant()` on every request through the
        member store, so a request that authenticates into `acme` must produce
        `acme` there. With `require_principal` synchronous this reads
        `default`.
        """
        observed: list[str] = []

        class Watching:
            def get(self, subject):
                observed.append(current_tenant())
                return None

            def touch(self, subject, email=""):
                observed.append(current_tenant())

        from app.authz.resolver import reset_resolver, reset_sightings
        from app.authz.store import set_member_store

        set_member_store(Watching())
        reset_resolver()
        reset_sightings()
        try:
            with self._client(monkeypatch, tmp_path) as client:
                response = client.get("/me", headers={"Authorization": "Bearer acme-tok"})
                assert response.status_code == 200
                assert response.json()["tenant"] == ACME
        finally:
            set_member_store(None)
            reset_resolver()

        assert observed, "the member store was never consulted; the test proves nothing"
        assert set(observed) == {ACME}, (
            f"a request from tenant {ACME!r} reached the store as {set(observed)}. "
            f"The ambient tenant is not surviving the authentication dependency, "
            f"so every tenant's rows land in one."
        )
