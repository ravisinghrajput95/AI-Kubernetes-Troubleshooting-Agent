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
