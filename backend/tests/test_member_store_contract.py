"""One suite, both member stores.

Role bindings are chosen by the same configuration that chooses the job store
and the enrolment store, and nothing above `MemberStore` can tell which it has —
which stays true only for as long as the two behave the same. These are the
guarantees both must provide.

The file store runs always; the Postgres store runs under
`K8S_AGENT_INTEGRATION=1`, the same precedent as
`test_enrolment_store_contract.py`. There is no fake Postgres, for the same
reason there is none there: the isolation this table needs is row-level
security, and a substitute would prove the substitute works.
"""

import pytest

from app.authz.models import Role
from app.authz.store import FileMemberStore
from tests.distributed_backend import INTEGRATION_ENABLED, SKIP_REASON, DistributedBackend

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture(params=["file", "postgres"])
async def store(request, tmp_path):
    if request.param == "file":
        yield FileMemberStore(tmp_path / "members.json")
        return

    if not INTEGRATION_ENABLED:
        pytest.skip(SKIP_REASON)

    backend = DistributedBackend(with_bus=False)
    try:
        yield backend.members()
    finally:
        await backend.close()


class TestGrantingARole:
    async def test_an_unknown_subject_has_no_membership(self, store):
        assert store.get(ALICE) is None

    async def test_a_grant_is_readable_back(self, store):
        store.upsert(ALICE, Role.ADMIN, email=ALICE, granted_by="olivia")
        member = store.get(ALICE)

        assert member.role is Role.ADMIN
        assert member.email == ALICE
        assert member.granted_by == "olivia"
        assert member.suspended is False
        assert member.assigned is True

    async def test_a_regrant_replaces_the_role(self, store):
        store.upsert(ALICE, Role.VIEWER)
        store.upsert(ALICE, Role.OPERATOR)
        assert store.get(ALICE).role is Role.OPERATOR

    async def test_a_regrant_without_an_email_keeps_the_known_one(self, store):
        """`rbacctl` grants by subject and has no email to offer."""
        store.upsert(ALICE, Role.VIEWER, email=ALICE)
        store.upsert(ALICE, Role.ADMIN)
        assert store.get(ALICE).email == ALICE

    async def test_removal_reports_whether_anything_was_there(self, store):
        assert store.remove(ALICE) is False
        store.upsert(ALICE, Role.VIEWER)
        assert store.remove(ALICE) is True
        assert store.get(ALICE) is None


class TestASightingCarriesNoAuthority:
    """The property the whole model rests on.

    Every authenticated request records a sighting so an admin can find real
    people. A row created that way must carry no role: written as `viewer` it
    would demote a caller holding the deployment default on their next request.
    """

    async def test_being_seen_creates_a_row_with_no_role(self, store):
        store.touch(ALICE, ALICE)
        member = store.get(ALICE)

        assert member is not None
        assert member.role is None
        assert member.assigned is False
        assert member.last_seen_at is not None

    async def test_being_seen_does_not_change_an_existing_role(self, store):
        store.upsert(ALICE, Role.ADMIN)
        store.touch(ALICE)
        assert store.get(ALICE).role is Role.ADMIN

    async def test_being_seen_records_an_email_the_grant_did_not_have(self, store):
        store.upsert(ALICE, Role.ADMIN)
        store.touch(ALICE, ALICE)
        assert store.get(ALICE).email == ALICE

    async def test_being_seen_does_not_lift_a_suspension(self, store):
        store.upsert(ALICE, Role.ADMIN)
        store.set_suspended(ALICE, True)
        store.touch(ALICE)
        assert store.get(ALICE).suspended is True


class TestSuspension:
    async def test_suspending_an_unknown_subject_reports_nothing(self, store):
        assert store.set_suspended(ALICE, False) is None

    async def test_a_suspension_round_trips(self, store):
        store.upsert(ALICE, Role.ADMIN)
        assert store.set_suspended(ALICE, True).suspended is True
        assert store.get(ALICE).suspended is True
        assert store.set_suspended(ALICE, False).suspended is False

    async def test_a_suspension_keeps_the_role(self, store):
        """Lifting it must restore what they had, not what a default says."""
        store.upsert(ALICE, Role.ADMIN)
        store.set_suspended(ALICE, True)
        assert store.get(ALICE).role is Role.ADMIN


class TestListingAndCounting:
    async def test_members_list_strongest_first(self, store):
        store.upsert(BOB, Role.VIEWER)
        store.upsert(ALICE, Role.OWNER)
        store.touch("carol@example.com")

        listed = [member.subject for member in store.list()]
        assert listed[0] == ALICE
        assert listed[-1] == "carol@example.com"

    async def test_owners_are_counted(self, store):
        assert store.count_owners() == 0
        store.upsert(ALICE, Role.OWNER)
        store.upsert(BOB, Role.OWNER)
        assert store.count_owners() == 2

    async def test_a_suspended_owner_is_not_counted(self, store):
        """The last-owner rule reads this; a suspended owner cannot unlock a tenant."""
        store.upsert(ALICE, Role.OWNER)
        store.set_suspended(ALICE, True)
        assert store.count_owners() == 0

    async def test_a_seen_only_row_is_not_an_owner(self, store):
        store.touch(ALICE)
        assert store.count_owners() == 0


class TestTheFileStoreRefusesToGuess:
    """A store that read as empty would grant the default role to everyone."""

    def test_an_unreadable_file_raises_rather_than_starting_empty(self, tmp_path):
        path = tmp_path / "members.json"
        path.write_text("{ this is not json", encoding="utf-8")

        with pytest.raises(RuntimeError, match="unreadable"):
            FileMemberStore(path).list()

    def test_bindings_survive_a_restart(self, tmp_path):
        """Why there is no in-memory member store.

        An operator who set `RBAC_DEFAULT_ROLE=viewer` and assigned roles by
        hand must not be locked out of their own platform by a restart.
        """
        path = tmp_path / "members.json"
        FileMemberStore(path).upsert(ALICE, Role.OWNER)
        assert FileMemberStore(path).get(ALICE).role is Role.OWNER

    def test_two_tenants_do_not_share_bindings(self, tmp_path):
        """The file store serves the single-tenant deployment, but `tenant_scope`
        is not gated by `TENANCY_MODE` — merging two tenants' role bindings into
        one dictionary would be a cross-tenant admin."""
        from app.tenancy import tenant_scope

        store = FileMemberStore(tmp_path / "members.json")
        with tenant_scope("acme"):
            store.upsert(ALICE, Role.OWNER)
        with tenant_scope("globex"):
            assert store.get(ALICE) is None
            assert store.list() == []
        with tenant_scope("acme"):
            assert store.get(ALICE).role is Role.OWNER
