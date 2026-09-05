"""The README has to stay true, and `docs/` has to stay reachable.

`docs/PRODUCTION_READINESS.md` carried "README documents a roadmap as if
implemented; `docs/` is orphaned" as an open finding for several milestones, and
both halves were measurable the whole time. Nine documents were unreachable from
the README — including the backup runbook and the data-protection statement,
which are exactly what someone deploying this would look for first. An
unreferenced document is one nobody reads and therefore one nobody updates,
which is how a doc becomes a stale claim; this repository treats those as
defects rather than as untidiness.

These are cheap, and they are the kind of check that only earns its place by
having failed once.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DOCS = ROOT / "docs"


def linked_documents() -> set[str]:
    return set(re.findall(r"\(docs/([A-Za-z_0-9-]+\.md)\)", README.read_text()))


def test_every_document_is_reachable_from_the_readme():
    present = {path.name for path in DOCS.glob("*.md")}
    orphaned = sorted(present - linked_documents())
    assert not orphaned, (
        f"{len(orphaned)} document(s) in docs/ are linked from nowhere in the "
        f"README: {orphaned}. Add them to the Documentation section, or delete "
        f"them — an unreferenced document stops being maintained and becomes a "
        f"stale claim."
    )


def test_the_readme_links_nothing_that_does_not_exist():
    present = {path.name for path in DOCS.glob("*.md")}
    dangling = sorted(linked_documents() - present)
    assert not dangling, f"the README links documents that do not exist: {dangling}"


def test_the_readme_does_not_quote_a_test_count_that_has_drifted():
    """It said 438 backend and 47 frontend tests for six milestones, against
    real counts three and five times those. A number in a README is a claim
    about the project's thoroughness, and a wrong one is worse than none."""
    text = README.read_text()
    for stale in ("438 tests", "47 tests"):
        assert stale not in text, (
            f"the README still quotes {stale!r}; either update it or stop "
            f"quoting a count that nothing keeps current"
        )


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        (
            "single-process with no HA",
            "M3 made any worker able to serve any investigation with DATABASE_URL "
            "and REDIS_URL set",
        ),
        (
            "No platform self-observability",
            "/metrics, phase timing and 17 alert rules ship",
        ),
        (
            "Proposed fleet architecture",
            "M4 through M9 built it; the architecture document is a design record, not a roadmap",
        ),
    ],
)
def test_the_readme_does_not_understate_what_exists(claim, why):
    """The unusual half of this finding.

    A README that oversells is the familiar failure. This one *undersold*: it
    told a reader the platform was single-process with no HA and no metrics,
    long after both shipped. Someone evaluating it would have discarded it for
    gaps that were closed, which is a worse outcome than the overselling
    everyone watches for.
    """
    assert claim not in README.read_text(), f"stale README claim {claim!r}: {why}"


def test_the_console_listens_for_every_event_the_stream_sends():
    """The console must register a listener for each name the server emits.

    `investigate.py` writes `event: <type>` on every frame, and a browser routes
    a *named* event only to a listener for that name — `onmessage` fires solely
    for the default, unnamed type. The console registered `onmessage` alone, so
    the stream opened, delivered nothing, errored, and fell back to polling.
    Every investigation it ever displayed was polled, and the only symptom was
    the "polling" tag appearing on a healthy run.

    Nothing could see it. `docs/INVESTIGATION_API.md` documented the right
    usage all along; the frontend's own tests passed because their
    `FakeEventSource.emit()` called `onmessage` directly, modelling a wire the
    server does not produce; and `integration_verify.sh` proves the *server*
    streams, with curl, which has no such dispatch rule.

    So this holds the console's list against the documented one. A type added
    to the server and not to the console goes back to being dropped in silence.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    documented = set(
        re.findall(
            r"`([a-z]+)`",
            re.search(
                r"Event types: (.*?)\. A `: keepalive`",
                (root / "docs/INVESTIGATION_API.md").read_text(),
                re.S,
            ).group(1),
        )
    )
    assert documented, "could not parse the event types out of INVESTIGATION_API.md"

    hook = (root / "frontend/src/hooks/useInvestigationJob.ts").read_text()
    block = re.search(r"STREAM_EVENT_TYPES = \[(.*?)\] as const", hook, re.S)
    assert block, "STREAM_EVENT_TYPES is gone from the hook; re-anchor this test"
    registered = set(re.findall(r'"([a-z]+)"', block.group(1)))

    assert registered == documented, (
        f"the console listens for {sorted(registered)} but the stream sends "
        f"{sorted(documented)}; the difference is delivered to nothing and the "
        f"console silently falls back to polling"
    )


def test_the_changelog_exists_and_names_the_current_release():
    """Held against the version the application actually serves.

    This asserted the literal `"0.1.0"` until v0.2.0, which passed the moment
    that section existed and then said nothing forever — a changelog lagging a
    release is exactly the stale-doc failure the rest of this file exists to
    catch, and the check meant to catch it could not. Reading `app.version`
    means bumping one without the other fails here.
    """
    from app.main import app

    changelog = ROOT / "CHANGELOG.md"
    assert changelog.exists(), "a tagged project needs a changelog"
    text = changelog.read_text()
    assert f"## [{app.version}]" in text, (
        f"the app reports version {app.version} and the changelog has no "
        f"section for it, so the release notes lag what is deployed"
    )
    assert "No production deployment exists" in text, (
        "the changelog must keep saying this while it is true; it is the single "
        "most important thing a reader deciding whether to trust a release needs"
    )
