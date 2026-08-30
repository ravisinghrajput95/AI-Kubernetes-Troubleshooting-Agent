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


def test_the_changelog_exists_and_names_the_current_release():
    changelog = ROOT / "CHANGELOG.md"
    assert changelog.exists(), "a tagged project needs a changelog"
    text = changelog.read_text()
    assert "0.1.0" in text
    assert "No production deployment exists" in text, (
        "the changelog must keep saying this while it is true; it is the single "
        "most important thing a reader deciding whether to trust a release needs"
    )
