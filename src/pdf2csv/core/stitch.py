"""Joining per-page tables into the logical tables a human would recognise.

A twelve-page statement is one table to the analyst and twelve tables to the
extractor. Getting from one to the other involves two decisions that are easy
to get quietly wrong:

1. **Which per-page tables belong together.** Grouping everything would merge a
   summary box into the transaction ledger. Grouping nothing leaves the analyst
   with twelve CSVs to concatenate by hand.

2. **Where the repeated headers are.** Page 2 onward repeat the column titles.
   Left in place they become data rows, and because ``parse_amount("Debit")``
   returns ``None`` rather than raising, they land as blanks — the column total
   then silently disagrees with the stated total by exactly the rows that were
   eaten. This is the single most common way a finance extraction produces
   numbers that look fine and are wrong.

Concatenate first, validate afterwards — never the reverse. Validating page by
page passes eleven times and misses the one continuity break that matters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pdf2csv.core.amounts import is_blank_marker, looks_numeric, parse_date
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import ExtractedTable, PageKind

log = get_logger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# How many of the opening rows may plausibly be header rather than data.
_MAX_HEADER_SCAN = 4


@dataclass
class StitchedTable:
    """One logical table, assembled from one or more pages.

    Row-parallel metadata (``row_pages``, ``row_kinds``, ``row_confidences``)
    is carried alongside the data so that a flag raised during validation can
    say *which page* a suspicious figure came from, and so OCR digit repair is
    applied only to rows that actually came from OCR.
    """

    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    row_pages: list[int] = field(default_factory=list)
    row_kinds: list[PageKind] = field(default_factory=list)
    row_confidences: list[list[float]] | None = None
    extractors: list[str] = field(default_factory=list)
    dropped_repeat_headers: int = 0

    @property
    def n_cols(self) -> int:
        return len(self.header) if self.header else max((len(r) for r in self.rows), default=0)

    @property
    def pages(self) -> list[int]:
        seen: list[int] = []
        for page in self.row_pages:
            if page not in seen:
                seen.append(page)
        return seen

    @property
    def has_ocr_rows(self) -> bool:
        return any(kind is PageKind.SCANNED for kind in self.row_kinds)


# --------------------------------------------------------------------------- #
# Header detection
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    """Aggressive normalisation for comparing header cells across pages.

    Deliberately lossy: ``"Débit (TND)"`` and ``"DEBIT TND"`` must compare
    equal, because OCR and typography will render the same header differently
    on different pages of the same document.
    """
    return _NON_ALNUM.sub("", text.casefold())


def _row_signature(row: list[str]) -> str:
    return "|".join(_norm(c) for c in row)


def score_header_row(row: list[str]) -> float:
    """How much this row looks like column titles rather than data, 0.0-1.0.

    Headers are short words, mostly unique, and contain neither amounts nor
    dates. Data rows are the opposite on every count.
    """
    cells = [c.strip() for c in row]
    filled = [c for c in cells if c]
    if not filled:
        return 0.0

    fill_ratio = len(filled) / len(cells)

    numeric = sum(1 for c in filled if looks_numeric(c))
    dated = sum(1 for c in filled if parse_date(c) is not None)
    wordy_ratio = 1.0 - (numeric + dated) / len(filled)

    unique_ratio = len({_norm(c) for c in filled}) / len(filled)

    # Titles are short. A 90-character cell is a description, not a heading.
    short_ratio = sum(1 for c in filled if len(c) <= 40) / len(filled)

    return 0.20 * fill_ratio + 0.45 * wordy_ratio + 0.20 * unique_ratio + 0.15 * short_ratio


def find_header(rows: list[list[str]]) -> tuple[list[str], int]:
    """Locate the header. Returns ``(header_cells, rows_consumed)``.

    ``rows_consumed`` is 0 when no row is convincing enough, in which case the
    caller generates positional names. Inventing a header out of the first data
    row would delete a transaction, which is far worse than an ugly column name.

    Two-row headers (``"Opening" / "Balance"`` stacked) are merged when the
    second row also scores as a header and would otherwise be read as data.
    """
    if not rows:
        return [], 0

    best_index, best_score = 0, 0.0
    for index in range(min(_MAX_HEADER_SCAN, len(rows))):
        score = score_header_row(rows[index])
        # Prefer the earliest strong candidate; later rows must beat it clearly.
        if score > best_score + (0.05 if index else 0.0):
            best_index, best_score = index, score

    if best_score < 0.62:
        log.debug("no convincing header row (best score %.2f)", best_score)
        return [], 0

    header = list(rows[best_index])
    consumed = best_index + 1

    # Stacked second header line, e.g. a unit row: "(TND)" under each amount.
    if consumed < len(rows):
        following = rows[consumed]
        if score_header_row(following) >= 0.62 and _looks_like_header_continuation(
            header, following
        ):
            header = [
                " ".join(part for part in (a.strip(), b.strip()) if part)
                for a, b in zip(header, following, strict=False)
            ]
            consumed += 1

    return header, consumed


def _looks_like_header_continuation(header: list[str], candidate: list[str]) -> bool:
    """A second header line fills gaps in the first, it does not repeat it."""
    if len(header) != len(candidate):
        return False
    gaps = sum(1 for cell in header if not cell.strip())
    filled_under_gaps = sum(
        1
        for top, bottom in zip(header, candidate, strict=False)
        if not top.strip() and bottom.strip()
    )
    return gaps > 0 and filled_under_gaps >= max(1, gaps // 2)


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


def group_tables(tables: list[ExtractedTable]) -> list[list[ExtractedTable]]:
    """Partition per-page tables into runs that are continuations of each other.

    Two tables continue each other when they have the same column count and
    appear on the same or consecutive pages. Column count is a blunt signal but
    a reliable one: a summary box and a transaction ledger essentially never
    share a width, and when they do, merging them is visible in the preview
    rather than silent in the totals.
    """
    groups: list[list[ExtractedTable]] = []

    for table in tables:
        if table.is_empty:
            continue
        if groups:
            previous = groups[-1][-1]
            same_width = previous.n_cols == table.n_cols
            adjacent = 0 <= table.page_number - previous.page_number <= 1
            if same_width and adjacent:
                groups[-1].append(table)
                continue
        groups.append([table])

    return groups


# --------------------------------------------------------------------------- #
# Stitching
# --------------------------------------------------------------------------- #


def stitch_group(group: list[ExtractedTable]) -> StitchedTable:
    """Merge one group into a single table, dropping repeated headers."""
    first = group[0]
    header, consumed = find_header(first.rows)
    header_signature = _row_signature(header) if header else ""

    result = StitchedTable(header=header, extractors=sorted({t.extractor for t in group}))

    confidences_available = any(t.confidences for t in group)
    collected_confidences: list[list[float]] = []

    for table_index, table in enumerate(group):
        start = consumed if table_index == 0 else 0
        for row_index in range(start, len(table.rows)):
            row = table.rows[row_index]

            # A row identical to the header is a repeat, wherever it appears.
            # Checking every row rather than only the first catches statements
            # that reprint the header mid-page after a section break.
            if header_signature and _row_signature(row) == header_signature:
                result.dropped_repeat_headers += 1
                continue

            if all(is_blank_marker(cell) or not cell.strip() for cell in row):
                continue

            result.rows.append(list(row))
            result.row_pages.append(table.page_number)
            result.row_kinds.append(table.kind)
            if confidences_available:
                if table.confidences and row_index < len(table.confidences):
                    collected_confidences.append(list(table.confidences[row_index]))
                else:
                    collected_confidences.append([1.0] * len(row))

    if confidences_available:
        result.row_confidences = collected_confidences

    # No header found: positional names, 1-based so they read naturally.
    if not result.header:
        width = max((len(r) for r in result.rows), default=0)
        result.header = [f"column_{i + 1}" for i in range(width)]

    _pad_to_header(result)

    if result.dropped_repeat_headers:
        log.info(
            "dropped %d repeated header row(s) across pages %s",
            result.dropped_repeat_headers,
            result.pages,
        )
    return result


def _pad_to_header(table: StitchedTable) -> None:
    """Force every row to the header width, so the dataframe build cannot fail."""
    width = len(table.header)
    for index, row in enumerate(table.rows):
        if len(row) < width:
            row.extend([""] * (width - len(row)))
        elif len(row) > width:
            # Extra cells are almost always a split artefact in the last column;
            # rejoining preserves the content instead of discarding it.
            merged = " ".join(part for part in row[width - 1 :] if part.strip())
            table.rows[index] = [*row[: width - 1], merged]
        if table.row_confidences is not None:
            confidence_row = table.row_confidences[index]
            if len(confidence_row) < width:
                confidence_row.extend([1.0] * (width - len(confidence_row)))
            else:
                table.row_confidences[index] = confidence_row[:width]


def stitch(tables: list[ExtractedTable]) -> list[StitchedTable]:
    """Full pass: group per-page tables, then stitch each group."""
    groups = group_tables(tables)
    stitched = [stitch_group(group) for group in groups]
    stitched = [t for t in stitched if t.rows]
    log.info(
        "stitched %d page table(s) into %d logical table(s)", len(tables), len(stitched)
    )
    return stitched


def pick_primary(tables: list[StitchedTable]) -> StitchedTable | None:
    """Choose the table the analyst almost certainly wants.

    Documents contain incidental tables — an address block, a fee schedule, a
    summary box. The transaction ledger is the one with the most data cells, and
    picking by that measure has been reliable enough that offering a chooser is
    a refinement rather than a necessity. The UI still lists the others.
    """
    if not tables:
        return None
    return max(tables, key=lambda t: sum(1 for row in t.rows for c in row if c.strip()))
