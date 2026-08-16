"""Data structures shared across the pipeline.

These are plain dataclasses with explicit ``to_dict`` methods rather than
pydantic models. The pipeline has to run inside an embeddable Python
distribution with no compiler available, so the fewer dependencies that reach
into the core, the better. FastAPI serialises these fine as plain dicts.

A note on ``ValidationReport``: it is the reason this project exists. A CSV
without one is an unreconciled number that looks authoritative, which is worse
than no number at all. Every path that writes a CSV also writes a report.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Page / table primitives
# --------------------------------------------------------------------------- #


class PageKind(str, Enum):
    """How a single page must be read.

    Classified per page, never per document: finance PDFs routinely staple a
    typed statement to a scanned annex, and treating the whole file as one kind
    either wastes minutes of OCR or silently drops the annex.
    """

    DIGITAL = "digital"
    """Has a usable text layer. Read with pdfplumber, costs milliseconds."""

    SCANNED = "scanned"
    """No usable text layer. Needs rasterising and OCR, costs 10-60 seconds."""

    EMPTY = "empty"
    """No text and no meaningful ink. Skipped entirely — usually a separator."""


class Severity(str, Enum):
    """How much a failed check should alarm the analyst."""

    ERROR = "error"
    """The numbers do not reconcile. Do not use this output as-is."""

    WARNING = "warning"
    """Something is suspicious but may be legitimate. Spot-check it."""

    INFO = "info"
    """Context, never a failure. Shown for the audit trail."""


@dataclass(slots=True)
class TextBox:
    """One OCR-recognised run of text, positioned on the page.

    Coordinates are in image pixels with the origin top-left, matching what
    OpenCV and RapidOCR both produce. The digital path never creates these.
    """

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float = 1.0

    @property
    def x_centre(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def y_centre(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": [self.x0, self.y0, self.x1, self.y1],
            "confidence": round(self.confidence, 4),
        }


@dataclass(slots=True)
class ExtractedTable:
    """A rectangular block of cells lifted off one page.

    ``rows`` is always rectangular by the time it leaves an extractor — short
    rows are right-padded with empty strings. Downstream code may assume that.

    ``confidences`` mirrors ``rows`` cell for cell when the table came from OCR,
    and is ``None`` for digital extraction where every character is exact.
    """

    page_number: int
    """1-based, matching what the analyst sees in a PDF reader."""

    kind: PageKind
    rows: list[list[str]] = field(default_factory=list)
    confidences: list[list[float]] | None = None
    extractor: str = "unknown"
    """Which strategy produced this: lattice, stream, ocr-grid, ocr-cluster."""

    bbox: tuple[float, float, float, float] | None = None

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    @property
    def is_empty(self) -> bool:
        return not any(any(c.strip() for c in row) for row in self.rows)

    def min_confidence(self) -> float:
        """Lowest cell confidence in the table, or 1.0 for digital tables."""
        if not self.confidences:
            return 1.0
        vals = [c for row in self.confidences for c in row if c is not None]
        return min(vals) if vals else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "kind": self.kind.value,
            "extractor": self.extractor,
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "min_confidence": round(self.min_confidence(), 4),
        }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Check:
    """One reconciliation result, phrased for someone who is not an engineer.

    ``title`` and ``hint`` are read directly by a finance analyst in the UI, so
    they must say what happened and what to do — never mention a function name,
    a column index, or a stack frame.
    """

    id: str
    title: str
    passed: bool
    severity: Severity = Severity.ERROR
    detail: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "passed": self.passed,
            "severity": self.severity.value,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass(slots=True)
class CellFlag:
    """A single cell worth a human's attention, addressed by row and column.

    Drives the highlighted cells in the preview grid. ``row`` indexes the
    exported dataframe, so a flag always points at something the analyst can
    find in the CSV they just downloaded.
    """

    row: int
    column: str
    reason: str
    severity: Severity = Severity.WARNING
    value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "column": self.column,
            "reason": self.reason,
            "severity": self.severity.value,
            "value": self.value,
        }


@dataclass
class ValidationReport:
    """The pass/fail gate that ships alongside every CSV.

    Failures do not block export. They annotate it. A blocked export teaches an
    analyst to work around the tool; a loud, specific warning teaches them to
    check the two rows that are actually wrong.
    """

    checks: list[Check] = field(default_factory=list)
    flags: list[CellFlag] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: _dt.datetime.now().astimezone().isoformat())

    @property
    def passed(self) -> bool:
        """True only when no ERROR-severity check failed.

        Warnings deliberately do not sink the report. If they did, every
        document with one fuzzy scanned digit would read as 'failed' and the
        distinction between 'check this cell' and 'these totals are wrong'
        would be lost.
        """
        return not any(
            (not c.passed) and c.severity is Severity.ERROR for c in self.checks
        )

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def add(
        self,
        id: str,
        title: str,
        passed: bool,
        *,
        severity: Severity = Severity.ERROR,
        detail: str = "",
        hint: str = "",
    ) -> Check:
        check = Check(
            id=id, title=title, passed=passed, severity=severity, detail=detail, hint=hint
        )
        self.checks.append(check)
        return check

    def flag(
        self,
        row: int,
        column: str,
        reason: str,
        *,
        severity: Severity = Severity.WARNING,
        value: str = "",
    ) -> None:
        self.flags.append(
            CellFlag(row=row, column=column, reason=reason, severity=severity, value=value)
        )

    def summary(self) -> str:
        """One line, suitable for a log or a CLI exit message."""
        if not self.checks:
            return "No checks ran — nothing in this document could be reconciled."
        failed = self.failed_checks
        if not failed:
            return f"All {len(self.checks)} checks passed."
        errors = sum(1 for c in failed if c.severity is Severity.ERROR)
        warnings = len(failed) - errors
        parts = []
        if errors:
            parts.append(f"{errors} failed")
        if warnings:
            parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        return f"{', '.join(parts)} out of {len(self.checks)} checks."

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary(),
            "generated_at": self.generated_at,
            "checks": [c.to_dict() for c in self.checks],
            "flags": [f.to_dict() for f in self.flags],
        }


# --------------------------------------------------------------------------- #
# Document + result
# --------------------------------------------------------------------------- #


@dataclass
class DocumentMeta:
    """Everything we learned about the source file, for the audit trail."""

    source_path: str = ""
    source_name: str = ""
    sha256: str = ""
    size_bytes: int = 0
    n_pages: int = 0
    page_kinds: list[PageKind] = field(default_factory=list)
    profile: str = "generic"
    duration_seconds: float = 0.0
    ocr_available: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def n_scanned(self) -> int:
        return sum(1 for k in self.page_kinds if k is PageKind.SCANNED)

    @property
    def n_digital(self) -> int:
        return sum(1 for k in self.page_kinds if k is PageKind.DIGITAL)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "n_pages": self.n_pages,
            "n_digital_pages": self.n_digital,
            "n_scanned_pages": self.n_scanned,
            "page_kinds": [k.value for k in self.page_kinds],
            "profile": self.profile,
            "duration_seconds": round(self.duration_seconds, 2),
            "ocr_available": self.ocr_available,
            "warnings": self.warnings,
        }


@dataclass
class ExtractionResult:
    """What :func:`pdf2csv.run` returns. The single currency of this codebase.

    ``dataframe`` is a real :class:`pandas.DataFrame`; it is typed ``Any`` here
    only so that importing :mod:`pdf2csv.models` does not drag pandas in.
    """

    dataframe: Any
    report: ValidationReport
    meta: DocumentMeta
    tables: list[ExtractedTable] = field(default_factory=list)
    """Per-page provenance: which strategy read which page, and how well."""

    extra_frames: list[Any] = field(default_factory=list)
    """Secondary tables found in the same document.

    A statement often carries a fee summary or a rate table beside the
    transaction ledger. ``dataframe`` is the one the analyst almost certainly
    wants; these are offered alongside rather than discarded, because throwing
    away a table the user can see in the PDF reads as a bug.
    """

    @property
    def n_rows(self) -> int:
        return 0 if self.dataframe is None else len(self.dataframe)

    @property
    def columns(self) -> list[str]:
        return [] if self.dataframe is None else [str(c) for c in self.dataframe.columns]

    def to_dict(self) -> dict[str, Any]:
        """The shape written to the ``.validation.json`` sidecar."""
        return {
            "tool": "pdf2csv",
            "n_rows": self.n_rows,
            "columns": self.columns,
            "document": self.meta.to_dict(),
            "validation": self.report.to_dict(),
            "tables": [t.to_dict() for t in self.tables],
            "extra_tables": [
                {"index": i + 1, "n_rows": len(f), "columns": [str(c) for c in f.columns]}
                for i, f in enumerate(self.extra_frames)
            ],
        }
