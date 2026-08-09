import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.core.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def single_process_state(monkeypatch):
    """Keep the application under test single-process.

    `build_state()` reads configuration at startup, so an ambient
    `DATABASE_URL` — exported by a developer, or set for the whole integration
    CI job — would otherwise make every `TestClient` install a Postgres-backed
    store into the process globals, and leave a closed pool behind for every
    test that followed.

    The distributed tests are unaffected: they build their backends from the
    environment directly rather than from settings, which is what lets both
    kinds of test run in one session.
    """
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "redis_url", "")


@pytest.fixture(autouse=True)
def fresh_authenticator():
    """No test inherits the authenticator another test built.

    The authenticator is a **process singleton constructed on first use**, so
    `monkeypatch.setattr(settings, "auth_mode", …)` changes the setting and
    leaves the cached object alone — and restoring the setting at teardown does
    not rebuild it either. A test that ran under `AUTH_MODE=token` therefore
    hands a `StaticTokenAuthenticator` to every test after it, whatever those
    tests believe they configured.

    Fixed here rather than in each fixture because the failure does not look
    like an authentication failure. `test_metrics.py` configures
    `AUTH_MODE=disabled`, inherited a token authenticator from
    `test_tenancy.py`, had every investigation 401 — and reported *missing
    instrumentation*, because counters that never move look exactly like
    counters that were never wired.

    It survived because pytest collects alphabetically and `test_metrics`
    sorts before `test_tenancy`, so a full run never hit the bad order. The
    security-relevant subset named in `SECURITY.md` lists them the other way
    round, which is how it surfaced. Predates the audit remediation entirely —
    reproduced at `9c55017`.
    """
    from app.auth.dependencies import reset_authenticator

    reset_authenticator()
    yield
    reset_authenticator()
