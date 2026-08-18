"""The one public entry point: :func:`run`.

The notebook calls it. The web UI calls it. The CLI calls it. The tests call
it. Nothing else in this project is allowed to contain extraction logic, which
is what makes "prototype here, deploy there" a non-event rather than a rewrite.

:func:`run` never raises for a document it merely dislikes. A PDF with no
tables, a scan on a machine without the OCR add-on, a page that fails to
parse — each produces a result carrying a failed check that says so in plain
words. It raises only when it cannot read the file at all, because that is the
one case where there is nothing useful to report.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from pdf2csv.config import get_settings
from pdf2csv.core import cache, digital, normalize, ocr, router, scanned, stitch, validate
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import (
    DocumentMeta,
    ExtractedTable,
    ExtractionResult,
    PageKind,
    Severity,
    TableResult,
    ValidationReport,
)
from pdf2csv.profiles import Profile, select_profile
from pdf2csv.wording import count, listed, plural

log = get_logger(__name__)

ProgressFn = Callable[[str, int, int, str], None]
"""``progress(stage, current, total, message)`` — drives the UI's progress bar.

Called often enough to feel live and cheaply enough to ignore. A callback that
raises is a bug in the caller, so exceptions are deliberately not swallowed.
"""


def _noop_progress(stage: str, current: int, total: int, message: str) -> None:
    return None


def run(
    pdf_path: str | Path,
    *,
    profile: str | Profile | None = None,
    progress: ProgressFn | None = None,
    enable_ocr: bool = True,
) -> ExtractionResult:
    """Extract, normalise and validate the tables in one PDF.

    Args:
        pdf_path: The document to read.
        profile: A profile name, a :class:`Profile`, or ``None`` to auto-detect.
        progress: Optional ``(stage, current, total, message)`` callback.
        enable_ocr: Set ``False`` to skip scanned pages entirely — useful when
            a caller knows the document is digital and wants a guaranteed-fast
            result.

    Returns:
        An :class:`ExtractionResult` carrying the dataframe, the validation
        report and the document metadata.

    Raises:
        FileNotFoundError: The path does not exist.
        ValueError: The file is not a readable PDF.
    """
    import pdfplumber

    emit = progress or _noop_progress
    settings = get_settings()
    started = time.perf_counter()

    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    report = ValidationReport()
    meta = DocumentMeta(
        source_path=str(path),
        source_name=path.name,
        size_bytes=path.stat().st_size,
        ocr_available=ocr.is_available(),
    )

    emit("reading", 0, 1, f"Opening {path.name}")
    meta.sha256 = cache.file_sha256(path)

    # pdfplumber is the preferred reader because it exposes the text layer, but
    # it is not the only one that can open a PDF. A real document in the sample
    # set reports zero pages through pdfplumber and renders perfectly through
    # pdfium, so a failure here is not the end of the road — it just means every
    # page has to be treated as an image.
    pdf = None
    try:
        pdf = pdfplumber.open(str(path))
        page_count = len(pdf.pages)
    except Exception as exc:
        log.info("pdfplumber could not open %s (%s); falling back to pdfium", path.name, exc)
        page_count = 0

    if page_count == 0:
        if pdf is not None:
            pdf.close()
        return _run_without_text_layer(
            path,
            meta=meta,
            report=report,
            settings=settings,
            profile=profile,
            enable_ocr=enable_ocr,
            emit=emit,
            started=started,
        )

    try:
        meta.n_pages = page_count
        if meta.n_pages > settings.max_pages:
            meta.warnings.append(
                f"Document has {meta.n_pages} pages; only the first "
                f"{settings.max_pages} were processed."
            )

        page_limit = min(meta.n_pages, settings.max_pages)

        # --- Route ----------------------------------------------------------
        emit("routing", 0, page_limit, "Deciding which pages need OCR")
        meta.page_kinds = router.classify_document(pdf, min_chars=settings.min_text_chars)[
            :page_limit
        ]

        resolved_profile = _resolve_profile(pdf, profile, page_limit)
        meta.profile = resolved_profile.name

        # --- Digital pages ---------------------------------------------------
        tables: list[ExtractedTable] = []
        digital_indices = [
            i for i, kind in enumerate(meta.page_kinds) if kind is PageKind.DIGITAL
        ]

        if digital_indices:

            def digital_progress(current: int, total: int, message: str) -> None:
                emit("digital", current, total, message)

            found = digital.extract_digital_tables(
                pdf,
                digital_indices,
                ragged_tolerance=settings.ragged_tolerance,
                progress=digital_progress,
            )
            tables.extend(found)

            # A page with an image and no extractable table is the signature of
            # a scan carrying a bad text layer from some earlier tool. Reroute
            # those, and only those, through OCR.
            produced = {t.page_number for t in found}
            for page_index in digital_indices:
                if router.needs_ocr_fallback(
                    pdf.pages[page_index], page_index + 1 in produced
                ):
                    log.info(
                        "page %d: text layer yielded no table, rerouting to OCR",
                        page_index + 1,
                    )
                    meta.page_kinds[page_index] = PageKind.SCANNED

        # --- Scanned pages ----------------------------------------------------
        scanned_indices = [
            i for i, kind in enumerate(meta.page_kinds) if kind is PageKind.SCANNED
        ]
        if scanned_indices:
            tables.extend(
                _run_scanned(
                    path,
                    scanned_indices,
                    meta=meta,
                    report=report,
                    settings=settings,
                    enable_ocr=enable_ocr,
                    emit=emit,
                )
            )
    finally:
        pdf.close()

    return _assemble(
        tables,
        path=path,
        meta=meta,
        report=report,
        settings=settings,
        resolved_profile=resolved_profile,
        emit=emit,
        started=started,
    )


def _run_without_text_layer(
    path: Path,
    *,
    meta: DocumentMeta,
    report: ValidationReport,
    settings,
    profile,
    enable_ocr: bool,
    emit: ProgressFn,
    started: float,
) -> ExtractionResult:
    """Read a PDF that pdfplumber cannot open, using pdfium and OCR alone.

    Reporting "contains no pages" for a document that a PDF reader opens
    happily is both wrong and useless. pdfium renders these files, so every
    page is treated as an image and put through the scanned path — which is
    what a document with no reachable text layer needs anyway.
    """
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path))
        page_count = len(document)
        close = getattr(document, "close", None)
        if callable(close):
            close()
    except Exception as exc:
        raise ValueError(
            f"{path.name} could not be opened as a PDF by any available reader: {exc}"
        ) from exc

    if page_count == 0:
        raise ValueError(f"{path.name} contains no pages.")

    meta.n_pages = page_count
    page_limit = min(page_count, settings.max_pages)
    meta.page_kinds = [PageKind.SCANNED] * page_limit
    meta.warnings.append(
        "This PDF has no text layer that could be read directly, so every page "
        "was processed as an image."
    )

    resolved_profile = select_profile("", requested=profile if isinstance(profile, str) else None)
    if isinstance(profile, Profile):
        resolved_profile = profile
    meta.profile = resolved_profile.name

    emit("routing", 0, page_limit, "No text layer; reading every page as an image")

    tables = _run_scanned(
        path,
        list(range(page_limit)),
        meta=meta,
        report=report,
        settings=settings,
        enable_ocr=enable_ocr,
        emit=emit,
    )

    return _assemble(
        tables,
        path=path,
        meta=meta,
        report=report,
        settings=settings,
        resolved_profile=resolved_profile,
        emit=emit,
        started=started,
    )


def _assemble(
    tables: list[ExtractedTable],
    *,
    path: Path,
    meta: DocumentMeta,
    report: ValidationReport,
    settings,
    resolved_profile,
    emit: ProgressFn,
    started: float,
) -> ExtractionResult:
    """Stitch, normalise and validate — shared by both ways in."""
    tables.sort(key=lambda t: t.page_number)

    # --- Assemble ------------------------------------------------------------
    emit("assembling", 0, 1, "Joining pages together")
    stitched = stitch.stitch(tables)
    primary = stitch.pick_primary(stitched)

    if primary is None:
        meta.duration_seconds = time.perf_counter() - started
        return _empty_result(report, meta, tables, resolved_profile)

    # --- Normalise and validate every table ---------------------------------
    # Every one, not only the chosen table. A document of assorted tables is
    # common, and a report attached to whichever table the analyst actually
    # wanted is worth far more than one attached to whichever was biggest.
    emit("validating", 0, 1, "Checking the numbers reconcile")

    ordered = [primary, *[t for t in stitched if t is not primary]]
    tables_out: list[TableResult] = []

    for table in ordered:
        table_report = report if table is primary else ValidationReport()
        try:
            normalised = normalize.normalize_table(
                table,
                table_report,
                profile=resolved_profile,
                low_confidence=settings.low_confidence,
            )
            validate.run_all(normalised, table_report, meta, resolved_profile)
        except Exception:
            # One awkward secondary table must not sink a document whose
            # primary extracted perfectly.
            log.debug("table on page(s) %s could not be processed", table.pages)
            continue

        tables_out.append(
            TableResult(
                index=len(tables_out),
                frame=normalised.frame,
                report=table_report,
                pages=table.pages,
                extractor=", ".join(table.extractors),
            )
        )

    if not tables_out:
        meta.duration_seconds = time.perf_counter() - started
        return _empty_result(report, meta, tables, resolved_profile)

    normalised_frame = tables_out[0].frame
    extras = [t.frame for t in tables_out[1:]]

    meta.duration_seconds = time.perf_counter() - started
    emit("done", 1, 1, report.summary())
    log.info(
        "extracted %d rows x %d columns from %s in %.1fs (%d table(s) found) — %s",
        len(normalised_frame),
        len(normalised_frame.columns),
        path.name,
        meta.duration_seconds,
        len(tables_out),
        report.summary(),
    )

    return ExtractionResult(
        dataframe=normalised_frame,
        report=report,
        meta=meta,
        tables=tables,
        extra_frames=extras,
        tables_out=tables_out,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_profile(pdf, profile: str | Profile | None, page_limit: int) -> Profile:
    """Pick the document profile, reading the opening pages if auto-detecting."""
    if isinstance(profile, Profile):
        return profile

    sample = ""
    try:
        for page in pdf.pages[: min(2, page_limit)]:
            sample += (page.extract_text() or "") + "\n"
    except Exception:
        sample = ""

    return select_profile(sample, requested=profile)


def _run_scanned(
    path: Path,
    scanned_indices: list[int],
    *,
    meta: DocumentMeta,
    report: ValidationReport,
    settings,
    enable_ocr: bool,
    emit: ProgressFn,
) -> list[ExtractedTable]:
    """Run the OCR path, degrading to a clear message when it cannot run."""
    page_list = listed([str(i + 1) for i in scanned_indices], limit=10)
    pages_word = plural(len(scanned_indices), "Page", "Pages")
    are_word = plural(len(scanned_indices), "is a scan", "are scans")

    if not enable_ocr:
        meta.warnings.append(f"Reading of scanned {page_list} was turned off for this run.")
        report.add(
            "ocr_skipped",
            "Scanned pages were not read",
            False,
            severity=Severity.WARNING,
            detail=f"{pages_word} {page_list} {are_word}, and reading scans was "
            "turned off for this run.",
            hint="Run it again with scanned-page reading enabled to include them.",
        )
        return []

    reason = ocr.unavailable_reason()
    if reason:
        meta.warnings.append(reason)
        report.add(
            "ocr_unavailable",
            "Scanned pages could not be read",
            False,
            severity=Severity.ERROR,
            detail=f"{pages_word} {page_list} {are_word}. {reason}",
            hint="Ask for a build that includes the scanned-document add-on. Until then, "
            "any figures on those pages are missing from this CSV.",
        )
        return []

    def relay(current: int, total: int, message: str) -> None:
        emit("ocr", current, total, message)

    try:
        return scanned.extract_scanned_tables(
            str(path),
            scanned_indices,
            dpi=settings.ocr_dpi,
            sha256=meta.sha256,
            progress=relay,
        )
    except Exception as exc:  # a broken scan must not lose the digital pages
        log.exception("OCR failed for %s", path.name)
        meta.warnings.append(f"OCR failed: {exc}")
        report.add(
            "ocr_failed",
            "Scanned pages could not be read",
            False,
            severity=Severity.ERROR,
            detail=f"{pages_word} {page_list} could not be processed: {exc}",
            hint="The rest of the document was still read. "
            "The log file has the technical detail.",
        )
        return []


def _empty_result(
    report: ValidationReport,
    meta: DocumentMeta,
    tables: list[ExtractedTable],
    profile: Profile,
) -> ExtractionResult:
    """A valid result that reports finding nothing, rather than an exception."""
    already_flagged = any(
        c.id in {"ocr_unavailable", "ocr_failed", "ocr_skipped"} and not c.passed
        for c in report.checks
    )
    report.add(
        "table_found",
        "A table was found and rows were extracted",
        False,
        severity=Severity.ERROR,
        detail=f"No table could be recognised in {count(meta.n_pages, 'page')}.",
        hint=(
            "See the scanned-pages message above."
            if already_flagged
            else "If you can see a table in the PDF, this layout is not yet supported. "
            "Send the file to whoever maintains this tool."
        ),
    )
    log.warning("no tables found in %s", meta.source_name)
    return ExtractionResult(
        dataframe=pd.DataFrame(), report=report, meta=meta, tables=tables
    )
