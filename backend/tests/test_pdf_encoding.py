"""The PDF must render the bytes it writes.

A hand-rolled PDF with base-14 fonts has one encoding trap and this repository
fell into it. Text is written with `.encode("latin-1")`, but a Type1 base-14
font with **no `/Encoding` entry** uses StandardEncoding, in which byte 0xE9 is
not `é` — it is `Ø`. So `Café` was stored correctly and rendered as mojibake,
silently, in the artefact most likely to be attached to a customer's incident
record.

Nothing errored, the file was valid, the byte count was right, and the existing
tests asserted the magic number and a 200. Found by inspecting a report from a
real investigation; see `docs/QA_AUDIT_2026-08-03.md`.
"""

import re

from app.services.history_service import InvestigationHistoryService

FONT_PATTERN = re.compile(rb"/Type\s*/Font[^>]*")


def pdf_for(*lines: str) -> bytes:
    service = InvestigationHistoryService.__new__(InvestigationHistoryService)
    return service._pdf_bytes([list(lines)])


class TestEveryFontDeclaresAnEncoding:
    def test_the_document_declares_win_ansi(self):
        assert b"/WinAnsiEncoding" in pdf_for("hello")

    def test_no_font_is_left_on_the_default_encoding(self):
        """The specific failure: it is not enough for *a* font to be encoded.
        Body text, headings and code each use a different font object, so one
        left bare corrupts one part of the page and nothing else."""
        fonts = FONT_PATTERN.findall(pdf_for("hello"))

        assert fonts, "no font objects found — the assertion below would be vacuous"
        unencoded = [f for f in fonts if b"/Encoding" not in f]
        assert not unencoded, f"fonts with no /Encoding: {unencoded}"


class TestAccentedTextSurvives:
    def test_latin1_bytes_are_written_for_accented_characters(self):
        """`é` must reach the file as the WinAnsi byte 0xE9 rather than being
        dropped or replaced. With an encoding declared, that byte now renders
        as the character it was written for."""
        assert "é".encode("latin-1") in pdf_for("Café naïve résumé")

    def test_accented_text_is_not_silently_replaced(self):
        rendered = pdf_for("Café")

        assert b"Caf?" not in rendered
        assert b"Caf" in rendered


class TestTheDocumentIsStillValid:
    """Adding tokens to a font dictionary is exactly the kind of edit that
    produces a subtly malformed file, so the structural guarantees are asserted
    alongside the encoding."""

    def test_it_is_still_a_pdf(self):
        assert pdf_for("hello").startswith(b"%PDF")

    def test_the_cross_reference_table_is_present(self):
        rendered = pdf_for("hello")

        assert b"xref" in rendered
        assert rendered.rstrip().endswith(b"%%EOF")

    def test_characters_outside_latin1_do_not_raise(self):
        """A real limit, pinned rather than left to be discovered: a base-14
        font cannot represent CJK or emoji, so those are transliterated or
        replaced. What must never happen is an exception mid-report."""
        rendered = pdf_for("日本語 🙂 café")

        assert rendered.startswith(b"%PDF")
