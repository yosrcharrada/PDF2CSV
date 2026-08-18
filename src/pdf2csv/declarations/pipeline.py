"""Running a declaration through the same result shape as everything else.

The web UI, the job runner, the preview grid, the checks panel and the download
buttons all speak :class:`~pdf2csv.models.ExtractionResult`. A declaration row
is a table — one row, twenty-two columns — so presenting it as one means the
entire front end works unchanged, and there is no second UI to keep in step
with the first.

Detection is deliberately cheap before it is expensive. Recognising a scan
costs seconds, so a document with a text layer is inspected for the words that
identify a declaration first, and only a document that looks like one (or that
has no text layer at all, and so might be) is put through OCR.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from pdf2csv.config import get_settings
from pdf2csv.core import cache
from pdf2csv.declarations.facts import extract_declarations
from pdf2csv.declarations.mapping import COLUMNS, DeclarationFacts, reconcile, to_row
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import (
    DocumentMeta,
    ExtractionResult,
    PageKind,
    Severity,
    TableResult,
    ValidationReport,
)
from pdf2csv.wording import count

log = get_logger(__name__)

__all__ = ["looks_like_declaration", "run_declaration"]

ProgressFn = Callable[[str, int, int, str], None]

_MARKERS = ("DECLARATION", "CERTIFICAT DE D", "FICHE DU SOUSCRIPTEUR")


def looks_like_declaration(pdf_path: str | Path) -> bool:
    """Is this worth putting through the declaration reader?

    Answered from the text layer when there is one, because that costs
    milliseconds against the seconds a recognition pass costs. A document with
    no readable text layer might be a scanned declaration, so it gets the
    benefit of the doubt — the reader returns nothing if it is not one, and the
    caller falls back to ordinary table extraction.
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            if len(pdf.pages) == 0:
                return True  # unreadable here but pdfium may still render it
            sample = " ".join((page.extract_text() or "") for page in pdf.pages[:2])
    except Exception:
        return True

    if not sample.strip():
        return True  # a scan: only OCR can tell

    upper = sample.upper()
    return any(marker in upper for marker in _MARKERS)


def run_declaration(
    pdf_path: str | Path,
    *,
    progress: ProgressFn | None = None,
    isin_pool: str | Path | None = None,
    dpi: int | None = None,
) -> ExtractionResult | None:
    """Read a declaration into the standard result shape, or ``None``.

    ``None`` means "this is not a declaration" — not an error. The caller falls
    back to ordinary table extraction, which is how one upload box serves both
    kinds of document.
    """
    started = time.perf_counter()
    path = Path(pdf_path)

    def emit(stage: str, current: int, total: int, message: str) -> None:
        if progress:
            progress(stage, current, total, message)

    emit("routing", 0, 1, "Checking whether this is a declaration")

    def relay(current: int, total: int, message: str) -> None:
        emit("ocr", current, total, message)

    facts_list = extract_declarations(
        str(path), dpi=dpi or 200, progress=relay
    )
    if not facts_list:
        return None

    emit("validating", 0, 1, "Checking the figures reconcile")

    report = ValidationReport()
    rows: list[dict] = []

    allocator = _Allocator.create(isin_pool)

    for facts in facts_list:
        isin, note = allocator.allocate(facts, report)
        rows.append(to_row(facts, isin=isin))

        for check in reconcile(facts):
            report.add(
                str(check["id"]),
                str(check["title"]),
                bool(check["passed"]),
                severity=Severity.ERROR if not check["passed"] else Severity.INFO,
                detail=str(check["detail"]),
                hint=(
                    ""
                    if check["passed"]
                    else "Compare this against the printed document before using the row."
                ),
            )
        if note:
            log.info("%s", note)

    frame = pd.DataFrame(rows, columns=COLUMNS)

    report.add(
        "declaration_read",
        "The declaration was read and mapped",
        True,
        severity=Severity.INFO,
        detail=(
            f"{count(len(rows), 'declaration row')}; "
            f"read with {min(f.confidence for f in facts_list):.0%} confidence."
        ),
    )
    _note_unresolved(report)

    meta = DocumentMeta(
        source_path=str(path),
        source_name=path.name,
        sha256=cache.file_sha256(path),
        size_bytes=path.stat().st_size,
        n_pages=len(facts_list),
        page_kinds=[PageKind.SCANNED] * len(facts_list),
        profile="declaration",
        duration_seconds=time.perf_counter() - started,
        ocr_available=True,
    )
    meta.warnings.extend(allocator.warnings)

    emit("done", 1, 1, report.summary())
    log.info("mapped %d declaration row(s) from %s", len(rows), path.name)

    return ExtractionResult(
        dataframe=frame,
        report=report,
        meta=meta,
        tables=[],
        tables_out=[
            TableResult(
                index=0,
                frame=frame,
                report=report,
                pages=[f.source_page for f in facts_list],
                extractor="declaration",
            )
        ],
    )


def _note_unresolved(report: ValidationReport) -> None:
    """Say plainly which columns are not yet trustworthy.

    Four mapping rules are still unconfirmed, and three of them produce a value
    that looks perfectly ordinary while being wrong. Saying so on every run is
    the only thing standing between that and someone filing the output.
    """
    report.add(
        "mapping_incomplete",
        "Some columns are not final yet",
        False,
        severity=Severity.WARNING,
        detail=(
            "code and BIC come from the subscriber's account, which is not in "
            "this PDF; nominal, auctionDate and amountToBePaid are still being "
            "confirmed against the finance team's reference files."
        ),
        hint="Check those columns by hand until the rules are confirmed.",
    )


class _Allocator:
    """ISIN allocation, degrading to an empty column when no pool is configured."""

    def __init__(self, pool=None, ledger=None) -> None:
        self.pool = pool
        self.ledger = ledger
        self.warnings: list[str] = []

    @classmethod
    def create(cls, isin_pool: str | Path | None) -> _Allocator:
        source = isin_pool or os.environ.get("PDF2CSV_ISIN_POOL", "").strip()
        if not source:
            return cls()

        try:
            from pdf2csv.declarations.isin import IsinLedger, IsinPool

            pool = IsinPool.load(source)
            ledger = IsinLedger.load(get_settings().home / "isin_ledger.json")
            return cls(pool, ledger)
        except Exception as exc:
            allocator = cls()
            allocator.warnings.append(f"ISIN pool unavailable: {exc}")
            log.warning("ISIN pool unavailable: %s", exc)
            return allocator

    def allocate(self, facts: DeclarationFacts, report: ValidationReport) -> tuple[str, str]:
        if self.pool is None or self.ledger is None:
            report.add(
                "isin_allocated",
                "An ISIN was assigned",
                False,
                severity=Severity.WARNING,
                detail="No ISIN workbook is configured, so this column is empty.",
                hint="Set PDF2CSV_ISIN_POOL to the 'block d ISIN' workbook to allocate codes.",
            )
            return "", ""

        from pdf2csv.declarations.isin import AllocationError, allocate

        try:
            isin, reused = allocate(facts, self.pool, self.ledger)
        except AllocationError as exc:
            report.add(
                "isin_allocated",
                "An ISIN was assigned",
                False,
                severity=Severity.ERROR,
                detail=str(exc),
                hint="Do not use this row until an ISIN has been assigned.",
            )
            return "", ""

        report.add(
            "isin_allocated",
            "An ISIN was assigned from the pool",
            True,
            severity=Severity.INFO,
            detail=(
                f"{isin} — already held by this issuance, not consumed again"
                if reused
                else f"{isin} — newly consumed from the pool"
            ),
        )
        return isin, f"{isin} ({'reused' if reused else 'new'})"


def today() -> dt.date:
    return dt.date.today()
