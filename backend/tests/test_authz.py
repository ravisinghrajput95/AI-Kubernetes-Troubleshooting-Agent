"""Who, inside a tenant, may do what.

M6 made a tenant a data boundary and stopped: inside one, every caller who could
authenticate could start investigations, mint cluster enrolment tokens and
revoke agent certificates. `Principal` already carried `groups` and they were
consulted only for Kubernetes impersonation.

Every test here is written so that it *can* fail. That is not rhetorical — a
permission check whose test passes with the check deleted is not a control, and
each class below names the mutation it is meant to catch. The mutations were
applied one at a time and the failures recorded before this file was committed.
"""

from itertools import pairwise

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.auth.dependencies import reset_authenticator
from app.auth.models import ANONYMOUS, Principal
from app.authz.models import (
    ROLE_PERMISSIONS,
    AuthorizationModelError,
    Grant,
    Permission,
    Role,
    highest,
)
from app.authz.resolver import (
    RoleResolver,
    default_role,
    parse_role_mappings,
    reset_resolver,
    reset_sightings,
    role_from_groups,
)
from app.authz.routes import AUTHENTICATED, PUBLIC, ROUTE_PERMISSIONS, required_permission
from app.authz.service import MemberError, assign_role, remove_role, set_suspended
from app.authz.store import FileMemberStore, set_member_store
from app.core.config import Settings, settings
from app.main import app
from tests.test_investigation_service import FakeKubectl

# One token per role, so a test names the role it is exercising.
TOKENS = ",".join(
    [
        "owner-tok:olivia@example.com",
        "admin-tok:alice@example.com",
        "operator-tok:oscar@example.com",
        "viewer-tok:victor@example.com",
        "nobody-tok:noel@example.com",
    ]
)

OWNER = {"Authorization": "Bearer owner-tok"}
ADMIN = {"Authorization": "Bearer admin-tok"}
OPERATOR = {"Authorization": "Bearer operator-tok"}
VIEWER = {"Authorization": "Bearer viewer-tok"}
NOBODY = {"Authorization": "Bearer nobody-tok"}

SUBJECTS = {
    "owner": "olivia@example.com",
    "admin": "alice@example.com",
    "operator": "oscar@example.com",
    "viewer": "victor@example.com",
}


@pytest.fixture
def members(tmp_path, monkeypatch):
    """A file-backed member store with one person per role."""
    monkeypatch.chdir(tmp_path)
    store = FileMemberStore(tmp_path / "members.json")
    store.upsert(SUBJECTS["owner"], Role.OWNER)
    store.upsert(SUBJECTS["admin"], Role.ADMIN)
    store.upsert(SUBJECTS["operator"], Role.OPERATOR)
    store.upsert(SUBJECTS["viewer"], Role.VIEWER)
    set_member_store(store)
    reset_sightings()
    yield store
    set_member_store(None)


@pytest.fixture
def api(members, monkeypatch):
    """The application, authenticating by token and denying unbound callers.

    `RBAC_DEFAULT_ROLE=none` here rather than the shipped `admin`, because these
    tests are about what each role may do; the shipped default is exercised on
    its own in `TestSingleTenantDeploymentsKeepWorking`.
    """
    monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
    monkeypatch.setattr(executor_module.KubectlExecutor, "failing_resources", set(), raising=False)
    monkeypatch.setattr(settings, "auth_mode", "token")
    monkeypatch.setattr(settings, "api_tokens", TOKENS)
    monkeypatch.setattr(settings, "impersonate_users", False)
    monkeypatch.setattr(settings, "rbac_default_role", "none")
    monkeypatch.setattr(settings, "oidc_role_mappings", "")
    reset_authenticator()
    reset_resolver()

    with TestClient(app) as client:
        yield client

    reset_authenticator()
    reset_resolver()
    app.dependency_overrides.clear()


# --- the model --------------------------------------------------------------


class TestTheRoleModel:
    def test_each_role_contains_the_one_below_it(self):
        """Roles are cumulative; a gap would be a permission only a lower role has."""
        order = [Role.VIEWER, Role.OPERATOR, Role.ADMIN, Role.OWNER]
        for lower, upper in pairwise(order):
            assert ROLE_PERMISSIONS[lower] < ROLE_PERMISSIONS[upper], f"{upper} lost {lower}'s"

    def test_every_permission_is_held_by_someone(self):
        """A permission no role grants is an endpoint nobody can ever reach."""
        granted = set().union(*ROLE_PERMISSIONS.values())
        assert set(Permission) == granted

    @pytest.mark.parametrize(
        "permission",
        [
            Permission.CLUSTER_ENROL,
            Permission.CLUSTER_REVOKE,
            Permission.MEMBER_MANAGE,
            Permission.INVESTIGATION_READ_ALL,
        ],
    )
    def test_lower_roles_hold_no_administrative_permission(self, permission):
        """Catches: adding an admin permission to a lower role."""
        assert not Role.VIEWER.permits(permission)
        assert not Role.OPERATOR.permits(permission)

    def test_only_an_owner_may_grant_ownership(self):
        assert Role.OWNER.permits(Permission.MEMBER_MANAGE_OWNER)
        assert not Role.ADMIN.permits(Permission.MEMBER_MANAGE_OWNER)

    def test_running_an_investigation_is_not_a_viewer_permission(self):
        """The viewer/operator boundary: the platform's only outbound action."""
        assert not Role.VIEWER.permits(Permission.INVESTIGATION_RUN)
        assert Role.OPERATOR.permits(Permission.INVESTIGATION_RUN)

    def test_the_strongest_grant_wins(self):
        assert highest(Role.VIEWER, Role.ADMIN) is Role.ADMIN
        assert highest(None, Role.VIEWER) is Role.VIEWER
        assert highest(None, None) is None


# --- where the check lives --------------------------------------------------


def _served_routes() -> list[tuple[str, str]]:
    """Every route the application serves, derived from the OpenAPI schema.

    Derived rather than listed, for the reason `test_auth.py` gives: the
    hand-maintained list it replaced had already drifted by four endpoints, and
    a new route must be covered the moment it exists rather than when somebody
    remembers.
    """
    from app.main import create_app

    schema = create_app().openapi()
    routes = []
    for path, operations in schema.get("paths", {}).items():
        if path in PUBLIC:
            continue
        for method in operations:
            if method.lower() in {"head", "options"}:
                continue
            routes.append((method.upper(), path))
    return sorted(set(routes))


SERVED = _served_routes()


class TestEveryRouteHasAPolicy:
    """The property that makes a forgotten endpoint fail closed."""

    def test_the_derived_route_list_is_not_empty(self):
        """A bug in the derivation would silently assert nothing."""
        assert len(SERVED) >= 18, SERVED

    @pytest.mark.parametrize("method,path", SERVED)
    def test_every_served_route_is_in_the_table(self, method, path):
        """Catches: adding an endpoint without deciding what it requires."""
        assert required_permission(method, path) is not None, (
            f"{method} {path} has no entry in app/authz/routes.py. A route with no "
            f"entry is denied at runtime; add one deliberately."
        )

    def test_the_table_has_no_entries_for_routes_that_do_not_exist(self):
        """A stale entry is a policy nobody is applying."""
        assert set(ROUTE_PERMISSIONS) <= set(SERVED)

    def test_a_route_absent_from_the_table_is_denied_not_allowed(self):
        """The load-bearing default. Catches: `.get(key, AUTHENTICATED)`."""
        assert required_permission("POST", "/some/endpoint/added/next/year") is None

    def test_only_two_routes_require_no_route_permission(self):
        """And the two mean different things, which is why both are named.

        `/me` has to be reachable so a locked-out user can see why. `/mcp`
        serves many capabilities through one endpoint, so its permission
        belongs to the *tool* — `test_mcp.py` asserts every tool has one, which
        is what stops `AUTHENTICATED` here from meaning "unchecked".

        Anything else appearing in this list is a route that quietly stopped
        requiring a permission.
        """
        open_routes = {route for route, need in ROUTE_PERMISSIONS.items() if need is AUTHENTICATED}
        assert open_routes == {("GET", "/me"), ("POST", "/mcp")}

    def test_every_data_router_installs_the_check(self):
        """Structural, because behaviour alone cannot see this.

        Handlers also depend on `require_principal`, so deleting
        `require_permission` from a router leaves authentication working and
        every 401 test passing — while the *authorisation* check silently stops
        running for every route on it. Only `health` is exempt, and it carries
        no data.

        Catches: `APIRouter(tags=[...])` without the dependency.
        """
        import app.api.agents
        import app.api.events
        import app.api.investigate
        import app.api.mcp
        import app.api.members
        import app.api.session
        from app.authz.dependencies import require_permission

        gated = {
            "investigate": app.api.investigate.router,
            "agents": app.api.agents.router,
            "session": app.api.session.router,
            "members": app.api.members.router,
            "mcp": app.api.mcp.router,
        }
        for name, router in gated.items():
            installed = [
                dependency.dependency for dependency in getattr(router, "dependencies", [])
            ]
            assert require_permission in installed, (
                f"the {name} router no longer installs require_permission; its "
                f"routes are authenticated but no longer authorised, and every "
                f"401 test still passes."
            )

        # Events is the one deliberate exception, and it is not an omission:
        # the caller carries no principal, the signature is the authorisation,
        # and `app/api/events.py` argues it.
        assert not getattr(app.api.events.router, "dependencies", [])

    def test_an_unmapped_route_is_refused_at_runtime(self, api, monkeypatch):
        """Not just the table — the dependency's behaviour on a miss.

        Catches: making the miss permissive. The route is removed from the
        table while the application is running, which is the closest reachable
        approximation of an endpoint that was never added to it.
        """
        monkeypatch.delitem(ROUTE_PERMISSIONS, ("GET", "/members"))
        reset_resolver()
        assert api.get("/members", headers=OWNER).status_code == 403


# --- what each role may actually do ------------------------------------------


class TestDestructiveOperationsAreGated:
    """Sharp edge 1: what a viewer gets when they call something they may not."""

    def test_a_viewer_cannot_mint_an_enrolment_token(self, api):
        response = api.post("/agents/enrolment", json={"cluster_id": "prod"}, headers=VIEWER)
        assert response.status_code == 403
        assert "cluster.enrol" in response.json()["detail"]

    def test_an_operator_cannot_mint_an_enrolment_token(self, api):
        assert (
            api.post("/agents/enrolment", json={"cluster_id": "prod"}, headers=OPERATOR).status_code
            == 403
        )

    def test_a_viewer_cannot_start_an_investigation(self, api):
        for path in ("/investigate", "/investigations"):
            response = api.post(path, json={"context": "test"}, headers=VIEWER)
            assert response.status_code == 403, path
            assert "investigation.run" in response.json()["detail"]

    def test_an_operator_can_start_an_investigation(self, api):
        assert (
            api.post("/investigations", json={"context": "test"}, headers=OPERATOR).status_code
            == 202
        )

    def test_a_viewer_may_still_read(self, api):
        assert api.get("/investigations", headers=VIEWER).status_code == 200
        assert api.get("/agents", headers=VIEWER).status_code == 200

    def test_an_operator_cannot_manage_members(self, api):
        assert api.get("/members", headers=OPERATOR).status_code == 403
        assert (
            api.put("/members/x@example.com", json={"role": "viewer"}, headers=OPERATOR).status_code
            == 403
        )

    def test_a_caller_with_no_role_is_denied_everything_but_me(self, api):
        assert api.get("/me", headers=NOBODY).status_code == 200
        assert api.get("/investigations", headers=NOBODY).status_code == 403
        assert api.get("/agents", headers=NOBODY).status_code == 403

    def test_me_says_why_a_denied_caller_is_denied(self, api):
        body = api.get("/me", headers=NOBODY).json()
        assert body["role"] == ""
        assert body["role_source"] == "none"
        assert body["permissions"] == []

    def test_me_carries_the_permission_list_the_console_gates_on(self, api):
        body = api.get("/me", headers=OPERATOR).json()
        assert body["role"] == "operator"
        assert "investigation.run" in body["permissions"]
        assert "cluster.enrol" not in body["permissions"]


class TestDenialIsAForbiddenNotANotFound:
    """403 for a permission, 404 for ownership — and in that order."""

    def test_a_permission_denial_is_403(self, api):
        assert (
            api.post("/agents/enrolment", json={"cluster_id": "prod"}, headers=VIEWER).status_code
            == 403
        )

    def test_permission_is_checked_before_ownership(self, api):
        """Catches: reversing the order.

        A caller who may not read investigations at all must not be able to use
        404-vs-403 to learn whether an id exists. Both a real-looking and an
        absent id have to answer identically, and they answer 403.
        """
        denied = api.get("/investigations/whatever/report", headers=NOBODY)
        missing = api.get(
            "/investigations/00000000-0000-0000-0000-000000000000/report", headers=NOBODY
        )
        assert denied.status_code == missing.status_code == 403

    def test_ownership_still_answers_404_for_a_caller_who_may_read(self, api):
        """The M6-era disclosure control is untouched by the new one."""
        assert api.get("/investigations/nope/report", headers=VIEWER).status_code == 404


class TestTheEventStreamIsOwnershipChecked:
    """The hole this milestone found.

    `GET /investigations/{id}/events` took only the store: every other
    investigation route takes the principal and 404s on a foreign id, and this
    one returned another user's live progress to anyone who knew the id.
    Authentication was applied at the router; authorisation simply was not.
    """

    def _submit(self, api, headers) -> str:
        return api.post("/investigations", json={"context": "test"}, headers=headers).json()["id"]

    def test_another_user_cannot_stream_your_investigation(self, api):
        job_id = self._submit(api, OPERATOR)
        # A second operator: allowed to run investigations, not to read this one.
        assert api.get(f"/investigations/{job_id}/events", headers=VIEWER).status_code == 404

    def test_the_owner_can_stream_their_own(self, api):
        job_id = self._submit(api, OPERATOR)
        with api.stream("GET", f"/investigations/{job_id}/events", headers=OPERATOR) as response:
            assert response.status_code == 200


class TestOnlyAnOwnerReadsTheWholeTenant:
    """`investigation.read_all`, and why it is owner-only rather than admin.

    The shipped default role is `admin`, which is what keeps existing
    single-tenant installs working unchanged. Put tenant-wide report reading in
    `admin` and that default silently removes the per-user report isolation
    those deployments already had — a confidentiality regression delivered by a
    milestone about tightening authorisation. It was caught by
    `test_auth.py::TestOwnership` failing, which is the whole reason that suite
    derives its assertions from behaviour rather than from a role table.
    """

    def test_an_admin_does_not_see_other_peoples_investigations(self, api):
        """Catches: adding `read_all` back to `admin`."""
        assert not Role.ADMIN.permits(Permission.INVESTIGATION_READ_ALL)

        api.post("/investigations", json={"context": "test"}, headers=OPERATOR)
        assert api.get("/investigation-jobs", headers=ADMIN).json()["items"] == []

    def test_an_operator_sees_only_their_own(self, api):
        api.post("/investigations", json={"context": "test"}, headers=OPERATOR)
        assert api.get("/investigations", headers=VIEWER).json()["items"] == []

    def test_an_owner_sees_the_tenants(self, api):
        api.post("/investigations", json={"context": "test"}, headers=OPERATOR)
        # The jobs list is the live half; history needs a finished run.
        items = api.get("/investigation-jobs", headers=OWNER).json()["items"]
        assert any(item["id"] for item in items)

    def test_read_all_does_not_authorise_a_write(self, api):
        """Regenerating is owner-scoped: a read permission must not permit a write."""
        job_id = api.post("/investigations", json={"context": "test"}, headers=OPERATOR).json()[
            "id"
        ]
        assert api.post(f"/investigations/{job_id}/regenerate", headers=OWNER).status_code == 404


# --- resolving a role --------------------------------------------------------


class TestGroupsDriveRoles:
    """Sharp edge: the customer's IdP, not a second directory."""

    def test_a_mapped_group_grants_its_role(self):
        resolver = RoleResolver(
            mappings=parse_role_mappings("sre=operator,platform=admin"),
            fallback=None,
            config=Settings(AUTH_MODE="token"),
        )
        grant = resolver.resolve(Principal(subject="a@x.com", groups=("sre",)))
        assert grant.role is Role.OPERATOR
        assert grant.source == "group"

    def test_the_strongest_matching_group_wins(self):
        mappings = parse_role_mappings("sre=operator,platform=admin")
        assert role_from_groups(("sre", "platform"), mappings) is Role.ADMIN

    def test_an_unmapped_group_grants_nothing(self):
        assert role_from_groups(("randoms",), parse_role_mappings("sre=operator")) is None

    def test_a_malformed_mapping_is_refused_at_startup(self):
        """Catches: skipping a bad entry, which silently makes admins viewers."""
        for raw in ["sre", "sre=", "=operator", "sre=wizard"]:
            with pytest.raises(AuthorizationModelError):
                parse_role_mappings(raw)

    def test_grants_combine_rather_than_override(self, members):
        """A stored binding must not be able to *lower* an IdP grant.

        That would be the second directory this design exists to avoid. Taking
        access away is `suspended`, which is not a role.
        """
        members.upsert("victor@example.com", Role.VIEWER)
        resolver = RoleResolver(
            mappings={"platform": Role.ADMIN},
            fallback=None,
            config=Settings(AUTH_MODE="token"),
        )
        grant = resolver.resolve(Principal(subject="victor@example.com", groups=("platform",)))
        assert grant.role is Role.ADMIN

    def test_an_assigned_role_applies_without_any_group(self, members):
        resolver = RoleResolver(mappings={}, fallback=None, config=Settings(AUTH_MODE="token"))
        grant = resolver.resolve(Principal(subject=SUBJECTS["admin"]))
        assert grant.role is Role.ADMIN
        assert grant.source == "assigned"


class TestSuspensionOverridesEverything:
    """The emergency stop. Catches: ignoring `suspended` in the resolver."""

    def test_a_suspended_member_is_denied_despite_their_groups(self, members):
        members.set_suspended(SUBJECTS["admin"], True)
        resolver = RoleResolver(
            mappings={"platform": Role.OWNER},
            fallback=Role.ADMIN,
            config=Settings(AUTH_MODE="token"),
        )
        grant = resolver.resolve(Principal(subject=SUBJECTS["admin"], groups=("platform",)))
        assert grant.role is None
        assert grant.source == "suspended"

    def test_suspension_is_denied_over_http(self, api, members):
        members.set_suspended(SUBJECTS["operator"], True)
        reset_resolver()
        response = api.post("/investigations", json={"context": "t"}, headers=OPERATOR)
        assert response.status_code == 403
        assert "suspended" in response.json()["detail"].lower()

    def test_a_suspension_can_be_lifted(self, api, members):
        members.set_suspended(SUBJECTS["operator"], True)
        reset_resolver()
        api.delete(f"/members/{SUBJECTS['operator']}/suspend", headers=ADMIN)
        assert (
            api.post("/investigations", json={"context": "t"}, headers=OPERATOR).status_code == 202
        )


class TestASightingGrantsNothing:
    """The bug this model was changed to prevent.

    Every authenticated request upserts a membership row so an admin can find
    real people. If that row carried a role, a caller holding the single-tenant
    default would be *demoted* to it on their very next request.
    """

    def test_being_seen_does_not_create_a_role(self, members):
        members.touch("newcomer@example.com", "newcomer@example.com")
        assert members.get("newcomer@example.com").role is None
        assert members.get("newcomer@example.com").assigned is False

    def test_a_default_role_survives_being_seen(self, tmp_path, monkeypatch):
        """Catches: `touch()` writing `viewer`."""
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "rbac_default_role", "admin")
        store = FileMemberStore(tmp_path / "members.json")
        set_member_store(store)
        try:
            resolver = RoleResolver(
                mappings={}, config=Settings(AUTH_MODE="token", RBAC_DEFAULT_ROLE="admin")
            )
            caller = Principal(subject="fresh@example.com")

            assert resolver.resolve(caller).role is Role.ADMIN
            store.touch(caller.subject)
            assert resolver.resolve(caller).role is Role.ADMIN
        finally:
            set_member_store(None)

    def test_being_seen_does_not_lift_a_suspension(self, members):
        members.upsert("bad@example.com", Role.VIEWER)
        members.set_suspended("bad@example.com", True)
        members.touch("bad@example.com")
        assert members.get("bad@example.com").suspended is True


# --- deployments -------------------------------------------------------------


class TestSingleTenantDeploymentsKeepWorking:
    """Sharp edge 3: same discipline as the single-process job store.

    Before roles existed, every authenticated caller in a single-tenant
    deployment could do everything. `RBAC_DEFAULT_ROLE=admin` is that behaviour
    preserved exactly, so no existing install has to be administered back into
    working order on upgrade.
    """

    def test_the_shipped_default_is_admin(self):
        assert default_role(Settings()) is Role.ADMIN

    def test_an_unbound_caller_can_still_do_everything(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "api_tokens", "tok:solo@example.com")
        monkeypatch.setattr(settings, "impersonate_users", False)
        monkeypatch.setattr(settings, "rbac_default_role", "admin")
        set_member_store(FileMemberStore(tmp_path / "members.json"))
        reset_authenticator()
        reset_resolver()
        reset_sightings()

        try:
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer tok"}
                assert client.get("/me", headers=headers).json()["role"] == "admin"
                assert (
                    client.post(
                        "/investigations", json={"context": "t"}, headers=headers
                    ).status_code
                    == 202
                )
        finally:
            set_member_store(None)
            reset_authenticator()
            reset_resolver()

    def test_the_check_still_runs_in_single_tenant_mode(self, tmp_path, monkeypatch):
        """Not switched off by mode.

        A control that only executes in the deployment nobody runs by default
        is the "present, correct and inert" failure this project already hit
        once with row-level security.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(settings, "auth_mode", "token")
        monkeypatch.setattr(settings, "api_tokens", "tok:solo@example.com")
        monkeypatch.setattr(settings, "tenancy_mode", "single")
        monkeypatch.setattr(settings, "rbac_default_role", "viewer")
        set_member_store(FileMemberStore(tmp_path / "members.json"))
        reset_authenticator()
        reset_resolver()
        reset_sightings()

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/agents/enrolment",
                    json={"cluster_id": "prod"},
                    headers={"Authorization": "Bearer tok"},
                )
                assert response.status_code == 403
        finally:
            set_member_store(None)
            reset_authenticator()
            reset_resolver()


class TestConfigurationRefusesAPermissiveDefault:
    """Catches: allowing `RBAC_DEFAULT_ROLE=admin` alongside TENANCY_MODE=shared."""

    @pytest.mark.parametrize("role", ["admin", "owner", "operator"])
    def test_a_powerful_default_is_refused_in_shared_mode(self, role):
        config = Settings(
            TENANCY_MODE="shared",
            DATABASE_URL="postgresql://localhost/x",
            AUTH_MODE="token",
            RBAC_DEFAULT_ROLE=role,
        )
        with pytest.raises(RuntimeError, match="RBAC_DEFAULT_ROLE"):
            config.validate_authz()

    @pytest.mark.parametrize("role", ["viewer", "none", ""])
    def test_a_read_only_default_is_allowed_in_shared_mode(self, role):
        """ "Everyone in the IdP may read their own tenant" is legitimate."""
        Settings(
            TENANCY_MODE="shared",
            DATABASE_URL="postgresql://localhost/x",
            AUTH_MODE="token",
            RBAC_DEFAULT_ROLE=role,
        ).validate_authz()

    def test_a_powerful_default_is_fine_in_single_tenant_mode(self):
        Settings(TENANCY_MODE="single", RBAC_DEFAULT_ROLE="admin").validate_authz()

    def test_a_malformed_group_mapping_is_refused_at_startup(self):
        with pytest.raises(AuthorizationModelError):
            Settings(OIDC_ROLE_MAPPINGS="sre").validate_authz()

    def test_an_unknown_default_role_is_refused(self):
        with pytest.raises(AuthorizationModelError):
            Settings(RBAC_DEFAULT_ROLE="superuser").validate_authz()

    def test_the_m6_refusals_are_untouched(self):
        """This milestone must not have relaxed anything M6 established."""
        with pytest.raises(RuntimeError, match="requires DATABASE_URL"):
            Settings(TENANCY_MODE="shared", DATABASE_URL="", AUTH_MODE="token").validate_tenancy()
        with pytest.raises(RuntimeError, match="requires authentication"):
            Settings(
                TENANCY_MODE="shared",
                DATABASE_URL="postgresql://localhost/x",
                AUTH_MODE="disabled",
            ).validate_tenancy()


class TestTheOpenDeploymentHole:
    """Sharp edge 4, such as it is.

    `AUTH_MODE=disabled` resolves the anonymous caller to `owner`. It is the
    authorisation counterpart of `system_scope()`: one function, one grep, one
    test. It is not a bypass — the permission check runs and the caller simply
    holds everything — and it changes nothing about a mode that already leaves
    every endpoint open.
    """

    def test_an_open_deployment_grants_everything(self):
        resolver = RoleResolver(mappings={}, config=Settings(AUTH_MODE="disabled"))
        grant = resolver.resolve(ANONYMOUS)
        assert grant.role is Role.OWNER
        assert grant.source == "open-deployment"

    def test_enrolment_is_still_refused_on_an_open_deployment(self, tmp_path, monkeypatch):
        """The M4b refusal is about the deployment, not the caller, and stays."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(settings, "auth_mode", "disabled")
        monkeypatch.setattr(settings, "allow_insecure_no_auth", True)
        monkeypatch.setattr(settings, "agent_gateway_port", 19999)
        reset_authenticator()
        reset_resolver()
        try:
            with TestClient(app) as client:
                response = client.post("/agents/enrolment", json={"cluster_id": "prod"})
                assert response.status_code == 403
                assert "authentication is disabled" in response.json()["detail"].lower()
        finally:
            reset_authenticator()
            reset_resolver()

    def test_authorisation_has_no_system_escape(self):
        """There is no `system_scope()` equivalent, and there must not be one.

        The tenancy escape exists because the queue consumer must read a row
        before it can know the tenant it names. Authorisation has no such
        ordering problem: the decision is made at the HTTP boundary and
        background work carries the principal it was submitted with. So the
        only place a role is manufactured rather than resolved is the open
        deployment above.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app"
        manufacturers = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "Role.OWNER" in path.read_text(encoding="utf-8")
        }

        assert manufacturers <= {
            "authz/models.py",  # defines it
            "authz/resolver.py",  # the open-deployment hole
            "authz/service.py",  # the last-owner rule
            "authz/store.py",  # counts owners; compares, never grants
            "api/members.py",  # gates granting it
        }, (
            f"Role.OWNER is now constructed in {manufacturers}. Authorisation "
            f"deliberately has no system escape; a new one needs to be argued for."
        )


# --- granting and removing ---------------------------------------------------


class TestYouCannotGrantARoleYouDoNotHold:
    """The reason `owner` exists as a role distinct from `admin`."""

    def test_an_admin_cannot_grant_owner(self, members):
        with pytest.raises(MemberError):
            assign_role("new@example.com", Role.OWNER, actor_role=Role.ADMIN, store=members)

    def test_an_admin_cannot_grant_owner_over_http(self, api):
        response = api.put("/members/new@example.com", json={"role": "owner"}, headers=ADMIN)
        assert response.status_code == 403

    def test_an_owner_can_grant_owner(self, api):
        response = api.put("/members/new@example.com", json={"role": "owner"}, headers=OWNER)
        assert response.status_code == 200
        assert response.json()["role"] == "owner"

    def test_an_admin_can_grant_up_to_admin(self, members):
        assert (
            assign_role("new@example.com", Role.ADMIN, actor_role=Role.ADMIN, store=members).role
            is Role.ADMIN
        )

    def test_a_caller_with_no_role_can_grant_nothing(self, members):
        with pytest.raises(MemberError):
            assign_role("new@example.com", Role.VIEWER, actor_role=None, store=members)

    def test_removing_someone_stronger_is_escalation_by_subtraction(self, members):
        """Catches: checking the rank only on grant, not on removal."""
        with pytest.raises(MemberError):
            remove_role(SUBJECTS["owner"], actor_role=Role.ADMIN, store=members)

    def test_suspending_someone_stronger_is_refused(self, members):
        with pytest.raises(MemberError):
            set_suspended(SUBJECTS["owner"], True, actor_role=Role.ADMIN, store=members)


class TestTheLastOwnerIsProtected:
    """Otherwise ownership is a state you can leave and cannot re-enter."""

    def test_the_last_owner_cannot_be_demoted(self, members):
        with pytest.raises(MemberError, match="last owner"):
            assign_role(SUBJECTS["owner"], Role.ADMIN, actor_role=Role.OWNER, store=members)

    def test_the_last_owner_cannot_be_removed(self, members):
        with pytest.raises(MemberError, match="last owner"):
            remove_role(SUBJECTS["owner"], actor_role=Role.OWNER, store=members)

    def test_the_last_owner_cannot_be_suspended(self, members):
        with pytest.raises(MemberError, match="last owner"):
            set_suspended(SUBJECTS["owner"], True, actor_role=Role.OWNER, store=members)

    def test_one_of_several_owners_can_be_demoted(self, members):
        assign_role("second@example.com", Role.OWNER, actor_role=Role.OWNER, store=members)
        assert (
            assign_role(SUBJECTS["owner"], Role.ADMIN, actor_role=Role.OWNER, store=members).role
            is Role.ADMIN
        )

    def test_a_suspended_owner_does_not_count_towards_the_floor(self, members):
        """Otherwise a suspended owner would keep a tenant permanently locked."""
        assign_role("second@example.com", Role.OWNER, actor_role=Role.OWNER, store=members)
        set_suspended("second@example.com", True, actor_role=Role.OWNER, store=members)
        assert members.count_owners() == 1

    def test_the_rule_survives_the_cli_path(self, members):
        """`rbacctl` skips escalation deliberately; it must not skip this."""
        with pytest.raises(MemberError, match="last owner"):
            remove_role(SUBJECTS["owner"], store=members, enforce_escalation=False)


class TestGrantingBeforeFirstLogin:
    """The decision instead of an invite flow.

    Pre-assignment is the entire useful content of an invite, without the
    email, the single-use token, the acceptance endpoint, or a second identity
    to reconcile against what the IdP eventually asserts.
    """

    def test_a_role_can_be_granted_to_someone_who_has_never_signed_in(self, api):
        response = api.put("/members/future@example.com", json={"role": "operator"}, headers=ADMIN)
        assert response.status_code == 200
        assert response.json()["role"] == "operator"

    def test_the_grant_applies_when_they_arrive(self, api, members):
        api.put("/members/noel@example.com", json={"role": "operator"}, headers=ADMIN)
        reset_resolver()
        assert api.get("/me", headers=NOBODY).json()["role"] == "operator"

    def test_removing_a_binding_does_not_remove_a_group_grant(self, members):
        """Which is correct: this table does not own that grant."""
        resolver = RoleResolver(
            mappings={"platform": Role.ADMIN}, fallback=None, config=Settings(AUTH_MODE="token")
        )
        remove_role(SUBJECTS["admin"], actor_role=Role.OWNER, store=members)
        grant = resolver.resolve(Principal(subject=SUBJECTS["admin"], groups=("platform",)))
        assert grant.role is Role.ADMIN


class TestTheStoreCannotBeReached:
    """Catches: falling back to the default role when the store fails.

    `RBAC_DEFAULT_ROLE=admin` and "the database is down" must not compose into
    "everybody is an admin".
    """

    def test_a_store_failure_denies_rather_than_defaults(self, api, monkeypatch):
        class Broken:
            def get(self, subject):
                raise RuntimeError("connection refused")

            def touch(self, subject, email=""):
                pass

        set_member_store(Broken())
        reset_resolver()
        try:
            assert api.get("/investigations", headers=ADMIN).status_code == 503
        finally:
            set_member_store(None)


class TestGrantDescription:
    def test_a_grant_reports_its_permissions(self):
        described = Grant(role=Role.VIEWER, source="assigned").to_dict()
        assert described["role"] == "viewer"
        assert "investigation.read" in described["permissions"]
        assert "cluster.enrol" not in described["permissions"]
