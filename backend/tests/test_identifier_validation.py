"""Identifiers are validated whole, including their last character.

`re.match` with `^...$` accepts a trailing newline, because `$` matches just
before one. So "prod\\n" was a valid cluster id and "acme\\n" a valid tenant — a
second identity rendering identically to the first everywhere a human reads it.
"""

import pytest

from app.security.identity import IdentityError, require_cluster_id, valid_cluster_id
from app.services.report_store import FilesystemReportStore, valid_investigation_id
from app.tenancy.models import TenantError, require_tenant_id, valid_tenant_id


@pytest.mark.parametrize("suffix", ["\n", "\r\n", " ", "\t"])
def test_a_cluster_id_with_trailing_whitespace_is_refused(suffix):
    assert not valid_cluster_id("prod" + suffix)
    with pytest.raises(IdentityError):
        require_cluster_id("prod" + suffix)


@pytest.mark.parametrize("suffix", ["\n", "\r\n", " "])
def test_a_tenant_id_with_trailing_whitespace_is_refused(suffix):
    assert not valid_tenant_id("acme" + suffix)
    with pytest.raises(TenantError):
        require_tenant_id("acme" + suffix)


def test_a_report_id_with_a_trailing_newline_is_refused(tmp_path):
    store = FilesystemReportStore(tmp_path)
    good = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    store.write(good, "json", b"{}")
    assert store.path(good, "json") is not None, "control: the plain id must resolve"
    assert store.path(good + "\n", "json") is None
    # The lookup above refuses it anyway (no such file exists), so the validator
    # is asserted on directly — otherwise the regex could regress unseen.
    assert valid_investigation_id(good)
    assert not valid_investigation_id(good + "\n")


def test_the_plain_forms_are_still_accepted():
    """The control, so a pattern that now refuses everything cannot pass."""
    assert valid_cluster_id("prod-eu-1.example")
    assert valid_tenant_id("acme")


def test_a_short_api_token_is_warned_about_without_being_logged():
    from loguru import logger

    from app.auth.authenticators import StaticTokenAuthenticator

    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        StaticTokenAuthenticator.from_config(
            "short-tok:alice@example.com," + "x" * 40 + ":bob@example.com"
        )
    finally:
        logger.remove(sink)

    warned = [line for line in lines if "shorter than" in line]
    assert len(warned) == 1 and "alice@example.com" in warned[0], (
        "exactly the short token's subject is warned about — and the long one is not"
    )
    assert "short-tok" not in " ".join(lines), "the token itself reached the log"
