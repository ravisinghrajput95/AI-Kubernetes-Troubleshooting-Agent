"""One suite, both enrolment stores.

Single-use enrolment and revocation are chosen by the same configuration that
chooses the job store, and the gateway cannot tell the two apart — which is only
true for as long as they behave the same. These are the guarantees both must
provide.

The file store runs always. The Postgres store runs when
`K8S_AGENT_INTEGRATION=1`, the same precedent as `test_job_store_contract.py`,
so an ordinary `python -m pytest` still needs no database and a divergence
between the two is caught by the same assertions rather than by a second suite
that drifts.

There is no fake Postgres here for the same reason there is none there: the
store's single-use guarantee *is* a conditional UPDATE, and a substitute would
prove the tests pass rather than that the store works.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.security.enrolment import FileEnrolmentStore, hash_token
from tests.distributed_backend import INTEGRATION_ENABLED, SKIP_REASON, DistributedBackend

CLUSTER = "prod-eu-1"


@pytest.fixture(params=["file", "postgres"])
async def store(request, tmp_path):
    if request.param == "file":
        yield FileEnrolmentStore(tmp_path / "enrolment.json")
        return

    if not INTEGRATION_ENABLED:
        pytest.skip(SKIP_REASON)

    backend = DistributedBackend(with_bus=False)
    try:
        yield backend.enrolment()
    finally:
        await backend.close()


def soon() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


class TestTokensAreSingleUse:
    async def test_a_token_is_spent_once_and_returns_its_cluster(self, store):
        token = store.issue_token(CLUSTER)

        assert store.spend_token(token) == CLUSTER
        assert store.spend_token(token) is None

    async def test_an_expired_token_is_refused(self, store):
        token = store.issue_token(CLUSTER, timedelta(seconds=-1))
        assert store.spend_token(token) is None

    async def test_an_unknown_token_is_refused(self, store):
        assert store.spend_token("k8sagt_never-issued") is None

    async def test_the_plaintext_token_is_never_retained(self, store):
        token = store.issue_token(CLUSTER)

        records = store.tokens(CLUSTER)
        assert len(records) == 1
        assert records[0].token_hash == hash_token(token)
        # Nothing in the record round-trips back to the credential.
        assert token not in str(records[0].describe())

    async def test_tokens_are_listed_per_cluster(self, store):
        store.issue_token(CLUSTER)
        store.issue_token("staging")

        assert {record.cluster_id for record in store.tokens(CLUSTER)} == {CLUSTER}
        assert len(store.tokens()) == 2

    async def test_a_spent_token_says_so(self, store):
        token = store.issue_token(CLUSTER)
        assert not store.tokens(CLUSTER)[0].spent

        store.spend_token(token)
        assert store.tokens(CLUSTER)[0].spent


class TestSingleUseHoldsUnderARace:
    """The reason `spend_token` is a conditional UPDATE and not read-then-write.

    Single-use is trivially true when one caller asks at a time, and that is
    not the case worth testing. Enrolment is the one moment an unauthenticated
    peer can obtain a credential, so what has to hold is that many simultaneous
    attempts on one token produce exactly one certificate. A read-then-write
    would pass every other test in this file and fail this one.
    """

    async def test_exactly_one_of_many_simultaneous_attempts_wins(self, store):
        token = store.issue_token(CLUSTER)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: store.spend_token(token), range(8)))

        assert outcomes.count(CLUSTER) == 1, f"the token was spent {outcomes.count(CLUSTER)} times"
        assert outcomes.count(None) == 7

    async def test_distinct_tokens_are_unaffected_by_each_other(self, store):
        """The mutual exclusion must be per token, not a global lock on enrolment."""
        tokens = [store.issue_token(f"cluster-{index}") for index in range(8)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(store.spend_token, tokens))

        assert sorted(outcomes) == sorted(f"cluster-{index}" for index in range(8))


class TestCertificatesAndRevocation:
    async def test_an_issued_certificate_is_recorded(self, store):
        record = store.record_certificate("aa11", CLUSTER, soon())

        assert record.serial == "aa11"
        assert record.cluster_id == CLUSTER
        assert not record.revoked
        assert [item.serial for item in store.certificates(CLUSTER)] == ["aa11"]

    async def test_revocation_is_reported_once(self, store):
        store.record_certificate("aa11", CLUSTER, soon())

        assert store.revoke_certificate("aa11", "compromised") is True
        assert store.revoke_certificate("aa11", "compromised") is False
        assert store.revoked_serials() == {"aa11"}

    async def test_revoking_an_unknown_serial_is_not_an_error(self, store):
        assert store.revoke_certificate("nothing") is False

    async def test_a_whole_cluster_can_be_revoked(self, store):
        store.record_certificate("aa11", CLUSTER, soon())
        store.record_certificate("bb22", CLUSTER, soon())
        store.record_certificate("cc33", "staging", soon())

        assert store.revoke_cluster(CLUSTER, "rotating") == 2
        assert store.revoked_serials() == {"aa11", "bb22"}

    async def test_an_expired_certificate_is_not_revocable(self, store):
        """TLS refuses it already; counting it would misreport the blast radius."""
        store.record_certificate("old", CLUSTER, datetime.now(UTC) - timedelta(days=1))
        assert store.revoke_cluster(CLUSTER) == 0

    async def test_the_reason_is_kept(self, store):
        store.record_certificate("aa11", CLUSTER, soon())
        store.revoke_certificate("aa11", "node compromised")

        record = store.certificates(CLUSTER)[0]
        assert record.revoked
        assert record.revoked_reason == "node compromised"

    async def test_certificates_outlive_their_validity_for_audit(self, store):
        """ "What identity was live on this date" is not answerable from a pruned table."""
        store.record_certificate("old", CLUSTER, datetime.now(UTC) - timedelta(days=1))
        assert [record.serial for record in store.certificates(CLUSTER)] == ["old"]
