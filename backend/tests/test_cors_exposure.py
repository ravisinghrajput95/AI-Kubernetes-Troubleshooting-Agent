"""Response headers the console actually reads must be readable by a browser.

CORS exposes only a handful of response headers to script by default. Every
other one is present on the wire and invisible to `fetch` — and that is not an
error anywhere: `headers.get(...)` returns `null`, which reads exactly like a
header the server never sent. Code that consumes it degrades quietly and
correctly, which is the shape of defect this repository keeps finding.

Two are consumed by the console:

- `Content-Disposition` names the file a report is saved as. The console reads
  it so the platform stays the one thing that names a report; unexposed, every
  download silently fell back to a generic name.
- `X-Correlation-ID` is the id an operator is invited to quote when reporting a
  problem, which they cannot do if it never reaches the page.

Asserted on the response the middleware produces, not on the settings object:
`expose_headers` correct in configuration and never passed to the middleware
reads identically to a working one.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import create_app

ORIGIN = "http://localhost:3000"


@pytest.fixture
def client(monkeypatch):
    # Settings are read at import, so the environment is too late; the rest of
    # the suite sets the attribute for the same reason.
    monkeypatch.setattr(settings, "auth_mode", "disabled")
    monkeypatch.setattr(settings, "allow_insecure_no_auth", True)
    with TestClient(create_app()) as test_client:
        yield test_client


def exposed(response) -> set[str]:
    raw = response.headers.get("access-control-expose-headers", "")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


class TestTheConsoleCanReadWhatItDependsOn:
    def test_the_report_filename_is_exposed_to_script(self, client):
        """Otherwise the console cannot read the name the platform chose."""
        response = client.get("/health", headers={"Origin": ORIGIN})
        assert "content-disposition" in exposed(response)

    def test_the_correlation_id_is_exposed_to_script(self, client):
        response = client.get("/health", headers={"Origin": ORIGIN})
        assert "x-correlation-id" in exposed(response)

    def test_a_cross_origin_response_carries_the_correlation_id_at_all(self, client):
        """The control: exposing a header the platform does not send proves nothing."""
        response = client.get("/health", headers={"Origin": ORIGIN})
        assert response.headers.get("X-Correlation-ID")
