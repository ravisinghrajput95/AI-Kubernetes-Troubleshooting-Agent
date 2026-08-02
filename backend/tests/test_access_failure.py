"""Telling "you may not read this cluster" from "this cluster is broken".

F6's finding, answered without the preflight it asked for. `kubectl auth can-i`
is a *command*, and `ResourceRequest` deliberately cannot carry one — that
closed verb set is what makes a request safe to send to a customer's cluster,
and an escape hatch added to improve an error message would be the worst trade
in this repository.

What the finding was actually about survives: with impersonation on, a user
whose Kubernetes RBAC is narrower than the platform's gets every read refused,
and the result used to read as a degraded investigation of a broken cluster.
The reads themselves are cheap — a 403 is immediate — so the cost was the
explanation, not the work.

The tests that matter here are the ones that stop it from over-firing. Telling
someone their permissions are wrong when they are fine sends them to their
platform team for nothing, and is worse than the generic message it replaces.
"""

import pytest

from app.kubernetes.access import FORBIDDEN_SHARE, access_failure


def coverage(**counts: int) -> dict:
    total = sum(counts.values())
    usable = counts.get("ok", 0) + counts.get("empty", 0)
    return {"total": total, "usable": usable, "by_status": counts}


class TestItRecognisesALockedDoor:
    def test_everything_forbidden_is_an_access_problem(self):
        message = access_failure(coverage(forbidden=12))
        assert message is not None
        assert "No cluster read succeeded" in message

    def test_it_names_the_impersonated_identity(self):
        """The confusing part is that the *platform* can read the cluster and
        the user cannot; naming them is what resolves it."""
        message = access_failure(
            coverage(forbidden=12), subject="victor@example.com", impersonating=True
        )
        assert "victor@example.com" in message
        assert "calling user" in message

    def test_it_offers_the_two_ways_out(self):
        message = access_failure(coverage(forbidden=12), subject="v@x.com", impersonating=True)
        assert "get/list" in message
        assert "IMPERSONATE_USERS=false" in message

    def test_without_impersonation_it_blames_the_platform_not_the_user(self):
        """Reads run as the service account there, so telling the user to fix
        their own RBAC would send them somewhere they cannot help."""
        message = access_failure(coverage(forbidden=12), subject="v@x.com", impersonating=False)
        assert "platform's own credentials" in message
        assert "v@x.com" not in message


class TestItDoesNotOverFire:
    """The failure mode that matters: a false accusation is worse than the
    generic message it replaces."""

    def test_no_forbidden_evidence_says_nothing(self):
        assert access_failure(coverage(ok=10, unavailable=2)) is None

    def test_one_refused_collector_is_a_gap_not_a_locked_door(self):
        """A user who can read pods but not nodes gets a genuinely degraded
        investigation, which is a normal and useful outcome."""
        assert access_failure(coverage(ok=10, forbidden=1)) is None

    def test_a_mostly_successful_read_says_nothing(self):
        assert access_failure(coverage(ok=8, forbidden=3)) is None

    def test_a_single_successful_read_is_enough_to_stay_quiet(self):
        """The guard that a first version lacked: it fired on share alone and
        claimed "every cluster read was refused" for a run where four had
        succeeded. Anything usable means the investigation has something to
        say, and this message would contradict it."""
        assert access_failure(coverage(ok=1, forbidden=20)) is None

    def test_a_partial_view_says_nothing(self):
        assert access_failure(coverage(ok=7, forbidden=6)) is None

    def test_an_empty_investigation_says_nothing(self):
        assert access_failure(coverage()) is None
        assert access_failure({}) is None

    def test_an_undeployed_backend_is_not_a_refusal(self):
        """`not_applicable` means the evidence never applied — an optional
        backend that is not installed. Counting it would make every cluster
        without Prometheus look like a permissions failure."""
        assert access_failure(coverage(ok=4, not_applicable=20, forbidden=1)) is None

    def test_undeployed_backends_do_not_dilute_a_real_refusal(self):
        """The exclusion, reached. The case above returns early on `usable`, so
        it never exercised the sum — a mutation replacing the filtered total
        with `sum(counts.values())` survived until this existed."""
        assert access_failure(coverage(not_applicable=40, forbidden=8)) is not None

    def test_too_few_refusals_to_diagnose(self):
        """ "Nothing usable, all refusals" is technically true of a scope that
        attempted one read. Saying "your RBAC is wrong" from one data point is
        a guess wearing a conclusion's clothes."""
        assert access_failure(coverage(forbidden=1)) is None
        assert access_failure(coverage(not_applicable=40, forbidden=2)) is None

    def test_a_cluster_that_is_merely_unreachable_says_nothing(self):
        """`unavailable` is a broken cluster, which is the thing this exists to
        be told apart from."""
        assert access_failure(coverage(unavailable=12)) is None


class TestTheThreshold:
    @pytest.mark.parametrize(
        "forbidden,others,expected",
        [
            (12, 0, True),  # nothing usable, all refused
            (9, 1, False),  # one read succeeded -> a partial view, not a door
            (6, 4, False),  # four succeeded -> emphatically not a door
            (1, 9, False),
        ],
    )
    def test_dominance_not_presence(self, forbidden, others, expected):
        result = access_failure(coverage(forbidden=forbidden, ok=others))
        assert (result is not None) is expected

    def test_an_unreachable_cluster_with_some_refusals_says_nothing(self):
        """Refusals must *dominate* the failures, or a cluster that is merely
        down gets diagnosed as a permissions problem — the same confusion,
        pointing the other way.

        Deliberately past `MINIMUM_REFUSALS`: with fewer, the floor returns
        first and this asserts nothing about the share test. A mutation
        disabling that test survived until this case existed.
        """
        assert access_failure(coverage(unavailable=20, forbidden=5)) is None

    def test_the_threshold_is_a_majority(self):
        """Documented as a value rather than buried, because loosening it is
        how this starts accusing people wrongly."""
        assert FORBIDDEN_SHARE >= 0.5


class TestItReachesTheInvestigation:
    """Wired into the health summary, so the job API's error carries it too:
    `collection_failure()` returns `health.message` when nothing was usable."""

    async def test_a_forbidden_cluster_reports_an_access_problem(self, monkeypatch):
        import app.kubernetes.kubectl_executor as executor_module
        from app.auth.models import Principal
        from app.core.config import settings
        from app.providers.local_kubectl import LocalKubectlProvider
        from app.services.investigation_service import InvestigationService
        from tests.test_investigation_service import FakeKubectl

        monkeypatch.setattr(settings, "impersonate_users", True)

        class Forbidden(FakeKubectl):
            def run(self, args, parse_json: bool = False):
                from app.kubernetes.kubectl_executor import KubectlResult

                return KubectlResult(
                    ["kubectl", *args],
                    False,
                    "",
                    "Error from server (Forbidden): pods is forbidden",
                    1,
                )

        service = InvestigationService(
            context="test-cluster",
            principal=Principal(subject="victor@example.com", auth_method="token"),
        )
        service.provider = LocalKubectlProvider(context="test-cluster", executor=Forbidden())
        investigation = await service.run()

        message = investigation["health"]["message"]
        assert investigation["health"]["status"] == "error"
        assert "victor@example.com" in message, message
        assert "No cluster read succeeded" in message

        assert executor_module is not None  # import kept meaningful

    async def test_a_healthy_cluster_is_unaffected(self, monkeypatch):
        """The guard that matters: this must not change any normal run."""
        from app.core.config import settings
        from app.providers.local_kubectl import LocalKubectlProvider
        from app.services.investigation_service import InvestigationService
        from tests.test_investigation_service import FakeKubectl

        monkeypatch.setattr(settings, "impersonate_users", False)
        service = InvestigationService(context="test-cluster")
        service.provider = LocalKubectlProvider(context="test-cluster", executor=FakeKubectl())
        investigation = await service.run()

        assert "No cluster read succeeded" not in investigation["health"]["message"]
