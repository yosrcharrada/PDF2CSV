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
import re
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from pdf2csv.config import get_settings
from pdf2csv.core import cache
from pdf2csv.declarations.facts import extract_declarations
from pdf2csv.declarations.mapping import (
    COLUMNS,
    DeclarationFacts,
    GroupTotals,
    amount_to_be_paid,
    certificate_count,
    isin_group_key,
    reconcile,
    to_row,
)
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
ProgressFn2 = Callable[[int, int, str], None]

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

    facts_list = _read(path, dpi=dpi or 200, relay=relay)
    if not facts_list:
        return None

    emit("validating", 0, 1, "Checking the figures reconcile")

    report = ValidationReport()
    rows: list[dict] = []

    allocator = _Allocator.create(isin_pool)

    # Rows describing one issuance share a single ISIN and a single pair of
    # totals, so the allocation and the totals are computed per group and not
    # per row. A single-subscriber declaration is the degenerate case of one
    # group of one, and comes out unchanged.
    groups: dict[tuple, list[DeclarationFacts]] = {}
    for facts in facts_list:
        groups.setdefault(isin_group_key(facts), []).append(facts)

    totals_for: dict[tuple, GroupTotals] = {
        key: GroupTotals(
            certificates=sum(certificate_count(f) for f in members),
            amount_to_be_paid=sum(amount_to_be_paid(f) for f in members),
        )
        for key, members in groups.items()
    }
    isin_for: dict[tuple, str] = {}

    for facts in facts_list:
        key = isin_group_key(facts)
        if key not in isin_for:
            isin_for[key], note = allocator.allocate(facts, report)
            if note:
                log.info("%s", note)
        rows.append(
            to_row(
                facts,
                isin=isin_for[key],
                totals=totals_for[key],
                tag=_instrument_tag(facts),
            )
        )

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
    frame = pd.DataFrame(rows, columns=COLUMNS)
    pages = max((f.page_count for f in facts_list), default=1)

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
    _note_unresolved(report, rows)

    meta = DocumentMeta(
        source_path=str(path),
        source_name=path.name,
        sha256=cache.file_sha256(path),
        size_bytes=path.stat().st_size,
        n_pages=pages,
        page_kinds=[PageKind.SCANNED] * pages,
        profile="fiche" if len(facts_list) > 1 or facts_list[0].subscriber else "declaration",
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


def _instrument_tag(facts: DeclarationFacts) -> str:
    """``'CD'`` where the document names the instrument, otherwise nothing.

    Field 2 carries the tag exactly when the source does: a fiche libelle reads
    ``SER BTKL 8.40% CD 31072026`` and its reference row keeps the ``CD``,
    while the CIL declaration names no instrument and its reference row has
    none. Reading it from the document reproduces both.
    """
    # Matched without relying on spaces: the recogniser returns the libelle
    # closed up, as "SERBTKL8.40%CD31072026", so a word-boundary search finds
    # nothing. Letters either side are excluded so that the CD of some longer
    # word cannot be mistaken for the tag.
    return "CD" if re.search(r"(?<![A-Z])CD(?![A-Z])", facts.libelle.upper()) else ""


def _read(path: Path, *, dpi: int, relay: ProgressFn2) -> list[DeclarationFacts]:
    """Read the document with whichever reader suits it, trying both.

    A fiche and a declaration cannot be told apart by their subject matter --
    both are certificates of deposit, and a declaration contains a block headed
    *fiche du souscripteur*. What separates them is that a declaration is
    titled ``DECLARATION`` and a fiche is not, so that is what picks the reader
    to try first.

    Both are tried either way. Choosing wrongly then costs a second recognition
    pass and never costs a wrong answer, which is the right way round: the
    documents come from outside and the cheap signal will eventually be wrong.
    """
    from pdf2csv.declarations.fiche import read_fiche

    def as_fiche() -> list[DeclarationFacts]:
        return read_fiche(path, dpi=dpi, progress=lambda c, t, m: relay(c, t, m))

    def as_declaration() -> list[DeclarationFacts]:
        return extract_declarations(str(path), dpi=dpi, progress=relay)

    first, second = (
        (as_declaration, as_fiche)
        if _titled_declaration(path)
        else (as_fiche, as_declaration)
    )
    found = first()
    return found if found else second()


def _titled_declaration(path: Path) -> bool:
    """Does the text layer, if there is one, call this a declaration?

    Scans have no text layer and answer ``False``, which sends them to the
    fiche reader first. That is the right default: the fiche reader recognises
    a ruled table with known headings and declines quickly on anything else,
    whereas the declaration reader accepts a much looser page.
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                return False
            text = " ".join((page.extract_text() or "") for page in pdf.pages[:2])
    except Exception:
        return False
    return "DECLARATION" in text.upper()


def _note_unresolved(report: ValidationReport, rows: list[dict]) -> None:
    """Say plainly which columns this document could not supply.

    Reported as checks rather than left to the documentation, because both are
    columns that look perfectly ordinary while being incomplete, and a row is
    filed by whoever is looking at this screen.
    """
    report.add(
        "settlement_account",
        "The settlement account is not in this document",
        False,
        severity=Severity.WARNING,
        detail=(
            "code is the subscriber's securities account and is printed "
            "nowhere in a declaration or a fiche, so it is left empty. BIC "
            "follows from it and defaults to the issuer's own, which is right "
            "wherever the subscriber holds with the issuer -- three of the "
            "four reference rows -- and wrong for a subscriber banking "
            "elsewhere. amountToBePaid is zero for the same reason."
        ),
        hint="Fill code, and check BIC and amountToBePaid, from the subscriber's account.",
    )

    identified = sum(1 for row in rows if row.get("nationalId") or row.get("lastName"))
    if identified:
        report.add(
            "subscriber_columns_filled",
            "The subscriber columns were filled from the document",
            True,
            severity=Severity.INFO,
            detail=(
                f"{count(identified, 'row')} carried a named subscriber, so "
                "columns 23-36 were written from it. The finance team's own "
                "reference file leaves those columns empty."
            ),
            hint="Clear them if the receiving system expects them blank.",
        )


class _Allocator:
    """ISIN allocation, degrading to an empty column when no pool is configured."""

    def __init__(self, pool=None, ledger=None) -> None:
        self.pool = pool
        self.ledger = ledger
        self.warnings: list[str] = []

    @classmethod
    def create(cls, isin_pool: str | Path | None) -> _Allocator:
        from pdf2csv.declarations.isin import discover_pool

        source = isin_pool or discover_pool()
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
                hint="Put the ISIN workbook in the isin/ folder, or set PDF2CSV_ISIN_POOL.",
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
