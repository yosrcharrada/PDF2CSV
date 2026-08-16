"""Table extraction from pages that have a real text layer.

Two mechanisms, chosen per page:

**Ruling lines.** When the document drew its own grid, follow it. Structure
that is literally printed on the page beats anything inferred from spacing, so
this is tried first and wins whenever it produces a clean result.

**Whitespace corridors.** For borderless tables — which is most bank
statements — columns are found by projecting every word onto the x-axis and
locating the vertical channels that no row crosses. This is the same algorithm
:mod:`pdf2csv.core.grid` uses on OCR output, deliberately shared, because the
problem is identical once you have positioned text.

Using our own corridor detection rather than pdfplumber's ``text`` strategy is
the important decision here, for one reason: **pdfplumber infers columns one
page at a time.** On a statement where the debit column happens to be empty on
page 1, page 1 comes back with five columns and page 2 with six, the two no
longer look like the same table, and the document silently splits in half.
Pooling the words from every page and deciding once removes that whole class of
failure — and it is exactly the "derive column boundaries from the whole
document" rule that matters just as much here as it does under OCR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pdf2csv.core import grid
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import ExtractedTable, PageKind, TextBox

log = get_logger(__name__)

# Ruled tables: follow the drawn lines.
LATTICE: dict[str, Any] = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "intersection_tolerance": 5,
    "snap_tolerance": 3,
    "join_tolerance": 3,
}

# Vertical rules, no horizontal ones. Common in statements: the columns are
# boxed but rows are separated only by leading.
MIXED: dict[str, Any] = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "text",
    "text_tolerance": 2,
    "intersection_tolerance": 5,
    "snap_tolerance": 3,
}

# pdfplumber's own borderless strategy. Kept strictly as a last resort, for
# pages where corridor detection finds fewer than two columns.
STREAM: dict[str, Any] = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "text_tolerance": 2,
    "intersection_tolerance": 5,
}

LINE_STRATEGIES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("lattice", LATTICE),
    ("mixed", MIXED),
)

_VIRTUAL_WIDTH = 1000.0
"""Shared coordinate space for pooling words across pages of differing size."""

_MAX_SANE_COLUMNS = 40
"""Beyond this, "columns" are words in a paragraph sliced vertically."""

_WHITESPACE = re.compile(r"\s+")


@dataclass
class _PageWords:
    """One digital page reduced to positioned words, awaiting column inference."""

    page_number: int
    width: float
    rows: list[list[TextBox]] = field(default_factory=list)
    page: Any = None
    band: tuple[int, int] | None = None
    """Which rows are the table body, as opposed to titles and footers."""

    def body_rows(self) -> list[list[TextBox]]:
        """Rows that vote on where the columns are — body only."""
        if self.band is None:
            return self.rows
        start, end = self.band
        return self.rows[start : end + 1]

    def table_rows(self) -> list[list[TextBox]]:
        """Rows that become the table: the body plus the header above it.

        One row of header is taken, not two. A stacked two-line header on a
        borderless page loses its top line, which costs a word in a column
        name; taking two rows instead would pull the account-number line into
        the data, which costs a corrupted row. The cheaper mistake wins.
        """
        if self.band is None:
            return self.rows
        start, end = self.band
        return self.rows[max(0, start - 1) : end + 1]


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


def _clean_cell(value: Any) -> str:
    """Normalise one cell. ``None`` means a spanned/merged cell in pdfplumber."""
    if value is None:
        return ""
    # A cell whose text wrapped onto two lines arrives with an embedded newline.
    # Collapsing to a single space keeps it in one cell, which is right — the
    # alternative, splitting on the newline, invents a row that does not exist.
    return _WHITESPACE.sub(" ", str(value)).strip()


def raggedness(raw_rows: list[list[Any]]) -> float:
    """How inconsistent the *raw* row widths are, from 0.0 (uniform) to 1.0.

    Measured before padding and on cell counts rather than filled cells. That
    distinction matters more than it looks: a bank statement fills either the
    debit column or the credit column on every row and never both, so scoring
    on filled cells reports a perfectly extracted statement as badly ragged and
    throws it away in favour of a merged mess.
    """
    if not raw_rows:
        return 1.0
    lengths = [len(row) for row in raw_rows]
    widest = max(lengths)
    if widest == 0:
        return 1.0
    return (widest - min(lengths)) / widest


def _rectangularise(rows: list[list[Any]]) -> list[list[str]]:
    """Clean every cell and right-pad short rows so the table is rectangular."""
    cleaned = [[_clean_cell(c) for c in row] for row in rows]
    width = max((len(r) for r in cleaned), default=0)
    for row in cleaned:
        row.extend([""] * (width - len(row)))
    return cleaned


def _drop_blank_edges(rows: list[list[str]]) -> list[list[str]]:
    """Remove entirely empty rows and columns.

    The text strategies in particular emit a phantom empty first column from
    the page margin. Left in place it becomes an unnamed column the analyst has
    to delete by hand every single time.
    """
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    width = len(rows[0])
    keep = [i for i in range(width) if any(row[i].strip() for row in rows)]
    if len(keep) == width:
        return rows
    return [[row[i] for i in keep] for row in rows]


def _usable(tables: list[list[list[str]]]) -> bool:
    """At least one table with two real rows and two real columns."""
    return any(len(t) >= 2 and len(t[0]) >= 2 for t in tables)


# --------------------------------------------------------------------------- #
# Line-based extraction
# --------------------------------------------------------------------------- #


def extract_with_lines(
    page: Any, *, ragged_tolerance: float
) -> tuple[list[list[list[str]]], str] | None:
    """Try the ruling-line strategies. Returns ``(tables, strategy)`` or ``None``.

    Accepts the first strategy that comes back uniform. There is no scoring
    here on purpose: a drawn grid is ground truth, and a result that follows it
    without raggedness is correct by construction, not merely the best of
    several guesses.
    """
    if not (getattr(page, "lines", None) or getattr(page, "rects", None)):
        return None

    for name, settings in LINE_STRATEGIES:
        try:
            raw = page.extract_tables(settings)
        except Exception as exc:
            log.debug("page %s: strategy %s raised: %s", page.page_number, name, exc)
            continue
        if not raw:
            continue

        if max((raggedness(t) for t in raw), default=1.0) > ragged_tolerance:
            continue

        tables = [_drop_blank_edges(_rectangularise(t)) for t in raw]
        tables = [t for t in tables if t]
        if _usable(tables):
            return tables, name

    return None


def extract_with_stream(page: Any) -> tuple[list[list[list[str]]], str] | None:
    """Last-resort fallback to pdfplumber's own borderless strategy."""
    try:
        raw = page.extract_tables(STREAM)
    except Exception:
        return None
    if not raw:
        return None
    tables = [_drop_blank_edges(_rectangularise(t)) for t in raw]
    tables = [t for t in tables if t and len(t[0]) <= _MAX_SANE_COLUMNS]
    return (tables, "stream") if _usable(tables) else None


# --------------------------------------------------------------------------- #
# Word-based extraction
# --------------------------------------------------------------------------- #


def page_words(page: Any) -> list[TextBox]:
    """Every word on the page as a positioned box.

    pdfplumber reports ``top``/``bottom`` measured downward from the top of the
    page, which is the same orientation OCR uses, so the boxes drop straight
    into the shared grid code with no coordinate flip.
    """
    try:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    except Exception as exc:
        log.debug("page %s: word extraction failed: %s", page.page_number, exc)
        return []

    boxes: list[TextBox] = []
    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        boxes.append(
            TextBox(
                text=text,
                x0=float(word["x0"]),
                y0=float(word["top"]),
                x1=float(word["x1"]),
                y1=float(word["bottom"]),
                confidence=1.0,
            )
        )
    return boxes


def _shared_boundaries(pending: list[_PageWords]) -> list[float] | None:
    """Column boundaries in virtual coordinates, pooled across every page."""
    pooled: list[list[TextBox]] = []
    for entry in pending:
        if entry.width <= 0:
            continue
        scale = _VIRTUAL_WIDTH / entry.width
        pooled.extend(
            [
                [
                    TextBox(
                        text=b.text,
                        x0=b.x0 * scale,
                        y0=b.y0,
                        x1=b.x1 * scale,
                        y1=b.y1,
                        confidence=b.confidence,
                    )
                    for b in row
                ]
                for row in entry.body_rows()
            ]
        )

    if not pooled:
        return None

    boundaries = grid.infer_column_boundaries(pooled, _VIRTUAL_WIDTH)
    if len(boundaries) - 1 < 2:
        return None

    log.info(
        "inferred %d shared column(s) from %d row(s) across %d page(s)",
        len(boundaries) - 1,
        len(pooled),
        len(pending),
    )
    return boundaries


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def extract_digital_tables(
    pdf: Any,
    page_indices: list[int],
    *,
    ragged_tolerance: float = 0.20,
    progress: Any = None,
) -> list[ExtractedTable]:
    """Read every digital page, using lines where drawn and corridors elsewhere.

    Two passes. The first settles the pages that have ruling lines and gathers
    words from the pages that do not; the second infers one shared set of
    column boundaries and applies it to all of them. Without the second pass,
    pages of the same statement disagree about how many columns they have.
    """
    tables: list[ExtractedTable] = []
    pending: list[_PageWords] = []

    for position, page_index in enumerate(page_indices):
        page = pdf.pages[page_index]
        page_number = page_index + 1
        if progress:
            progress(position, len(page_indices), f"Reading page {page_number}")

        ruled = extract_with_lines(page, ragged_tolerance=ragged_tolerance)
        if ruled is not None:
            found, strategy = ruled
            tables.extend(
                ExtractedTable(
                    page_number=page_number,
                    kind=PageKind.DIGITAL,
                    rows=rows,
                    confidences=None,  # digital text is exact
                    extractor=strategy,
                )
                for rows in found
            )
            log.info("page %d: %d table(s) via %s", page_number, len(found), strategy)
            _flush(page)
            continue

        words = page_words(page)
        if words:
            rows = grid.group_rows(words)
            pending.append(
                _PageWords(
                    page_number=page_number,
                    width=float(page.width),
                    rows=rows,
                    page=page,
                    band=grid.select_table_band(rows),
                )
            )
        else:
            _flush(page)

    if not pending:
        return tables

    boundaries = _shared_boundaries(pending)

    for entry in pending:
        built = _build_from_words(entry, boundaries)
        if built is not None:
            tables.append(built)
        else:
            fallback = extract_with_stream(entry.page)
            if fallback is not None:
                found, strategy = fallback
                tables.extend(
                    ExtractedTable(
                        page_number=entry.page_number,
                        kind=PageKind.DIGITAL,
                        rows=rows,
                        confidences=None,
                        extractor=strategy,
                    )
                    for rows in found
                )
                log.info("page %d: %d table(s) via %s", entry.page_number, len(found), strategy)
        _flush(entry.page)

    tables.sort(key=lambda t: t.page_number)
    return tables


def _build_from_words(entry: _PageWords, boundaries: list[float] | None) -> ExtractedTable | None:
    """Lay one page's words onto the shared column boundaries."""
    if boundaries is None or entry.width <= 0:
        return None

    scale = entry.width / _VIRTUAL_WIDTH
    page_boundaries = [b * scale for b in boundaries]

    text_grid, _confidences = grid.build_grid(entry.table_rows(), page_boundaries)
    text_grid = _drop_blank_rows_only(text_grid)
    if len(text_grid) < 2 or len(text_grid[0]) < 2:
        return None

    log.info(
        "page %d: %dx%d table via text-columns",
        entry.page_number,
        len(text_grid),
        len(text_grid[0]),
    )
    return ExtractedTable(
        page_number=entry.page_number,
        kind=PageKind.DIGITAL,
        rows=text_grid,
        confidences=None,
        extractor="text-columns",
    )


def _drop_blank_rows_only(rows: list[list[str]]) -> list[list[str]]:
    """Drop empty rows but keep every column.

    Columns must survive even when empty on this page: they are shared across
    the document, and dropping one here is what makes page 1 and page 2 stop
    matching.
    """
    return [row for row in rows if any(c.strip() for c in row)]


def _flush(page: Any) -> None:
    """Release pdfplumber's per-page caches, keeping memory flat on long docs."""
    flush = getattr(page, "flush_cache", None)
    if callable(flush):
        flush()
