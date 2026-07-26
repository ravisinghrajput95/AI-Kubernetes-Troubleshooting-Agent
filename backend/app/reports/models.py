"""Structured incident report.

Separates *what goes in the report* from *how it is rendered*, so the PDF,
Markdown and JSON writers present one composition rather than each assembling
their own view of the investigation.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ReportField:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class ReportSection:
    """One titled section. `body` is pre-flattened lines for the renderers."""

    title: str
    body: tuple[str, ...] = ()
    fields: tuple[ReportField, ...] = ()
    table: tuple[tuple[str, ...], ...] = ()
    headers: tuple[str, ...] = ()
    note: str = ""

    @property
    def empty(self) -> bool:
        return not (self.body or self.fields or self.table or self.note)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": list(self.body),
            "fields": [{"label": f.label, "value": f.value} for f in self.fields],
            "table": [list(row) for row in self.table],
            "headers": list(self.headers),
            "note": self.note,
        }

    def as_lines(self) -> list[str]:
        """Flattened rendering, for writers that only take lines of text."""
        lines: list[str] = []
        for item in self.fields:
            lines.append(f"{item.label}: {item.value}")
        lines.extend(self.body)
        for row in self.table:
            lines.append(" | ".join(row))
        if self.note:
            lines.append(self.note)
        return lines


@dataclass(frozen=True)
class IncidentReport:
    incident_id: str
    title: str
    generated_at: str
    sections: tuple[ReportSection, ...] = field(default=())

    def section(self, title: str) -> ReportSection | None:
        return next((item for item in self.sections if item.title == title), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "generated_at": self.generated_at,
            "sections": [section.to_dict() for section in self.sections],
        }
