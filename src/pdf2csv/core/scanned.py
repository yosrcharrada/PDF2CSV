"""The scanned path: rasterise, straighten, recognise once, rebuild the table.

Order of operations, and why each step is where it is:

1. **Rasterise at 300 DPI.** The floor for statement fonts — below it small
   digits lose strokes and 8 starts reading as 3. Above 400 the time doubles
   for a fraction of a percent of accuracy.
2. **Deskew.** Cheap, and it improves both recognition and row grouping. A two
   degree tilt is invisible to a human and pulls a row's text far enough
   vertically across the page that it stops grouping as one row.
3. **Detect ruling lines.** Where the document drew its own structure, use it.
   Inferring boundaries that are already printed on the page is strictly worse.
4. **OCR the whole page, once.** Never cell by cell. Cropping and recognising
   hundreds of cells individually multiplies runtime by roughly ten *and*
   produces worse text, because the recogniser loses the context it uses to
   disambiguate digits.
5. **Assign boxes to cells**, using rules where they exist and whitespace
   corridors where they do not.

Column boundaries are derived from the **whole document**, not per page. Per-page
inference drifts: page 4 finds its corridors a few pixels left of page 3's, and
the columns stop lining up when the pages are concatenated. Pages are therefore
scanned first and assembled second.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pdf2csv.core import cache, grid, ocr
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import ExtractedTable, PageKind, TextBox

log = get_logger(__name__)

_VIRTUAL_WIDTH = 1000.0
"""Common coordinate space for pooling boxes across pages of differing size."""

ProgressFn = Callable[[int, int, str], None]


@dataclass
class PageScan:
    """Everything learned about one scanned page before assembly."""

    page_number: int
    width: float
    height: float
    boxes: list[TextBox] = field(default_factory=list)
    x_rules: list[float] = field(default_factory=list)
    y_rules: list[float] = field(default_factory=list)
    skew: float = 0.0
    from_cache: bool = False

    @property
    def has_vertical_rules(self) -> bool:
        return len(self.x_rules) >= 3

    def to_payload(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "boxes": self.boxes,
            "x_rules": self.x_rules,
            "y_rules": self.y_rules,
            "skew": self.skew,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PageScan:
        return cls(
            page_number=payload["page_number"],
            width=payload["width"],
            height=payload["height"],
            boxes=payload.get("boxes", []),
            x_rules=payload.get("x_rules", []),
            y_rules=payload.get("y_rules", []),
            skew=payload.get("skew", 0.0),
            from_cache=True,
        )


# --------------------------------------------------------------------------- #
# Rendering and scanning
# --------------------------------------------------------------------------- #


def render_page(document: Any, page_index: int, dpi: int) -> Any:
    """Rasterise one page to a grayscale numpy array.

    Grayscale rather than colour: OCR and line detection both discard colour
    anyway, and it cuts the bitmap to a third of the memory. An A4 page at 300
    DPI is 2480x3508, which is 8.7 MB grayscale and 26 MB in RGB — the
    difference matters when a 200-page document is being processed on a desktop
    with other work open.
    """
    page = document[page_index]
    bitmap = page.render(scale=dpi / 72.0, grayscale=True)
    try:
        image = bitmap.to_pil()
        return grid.to_grayscale_array(image)
    finally:
        # pypdfium2 frees these on collection, but a 200-page loop should not
        # wait for the collector to notice several hundred megabytes.
        close = getattr(bitmap, "close", None)
        if callable(close):
            close()
        close = getattr(page, "close", None)
        if callable(close):
            close()


def scan_page(document: Any, page_index: int, *, dpi: int, sha256: str) -> PageScan:
    """Render, deskew, find rules and recognise one page — or reuse the cache."""
    page_number = page_index + 1

    cached = cache.load_page_scan(sha256, page_index, dpi)
    if cached is not None:
        log.info("page %d: reusing cached scan", page_number)
        return PageScan.from_payload(cached)

    image = render_page(document, page_index, dpi)
    image, skew = grid.deskew(image)
    y_rules, x_rules = grid.detect_rules(image)

    boxes = ocr.recognise(image)
    log.info(
        "page %d: OCR found %d text box(es); %d horizontal / %d vertical rule(s)",
        page_number,
        len(boxes),
        len(y_rules),
        len(x_rules),
    )

    scan = PageScan(
        page_number=page_number,
        width=float(image.shape[1]),
        height=float(image.shape[0]),
        boxes=boxes,
        x_rules=x_rules,
        y_rules=y_rules,
        skew=skew,
    )
    cache.save_page_scan(sha256, page_index, dpi, scan.to_payload())
    return scan


# --------------------------------------------------------------------------- #
# Document-wide column inference
# --------------------------------------------------------------------------- #


def _scaled_rows(rows: list[list[TextBox]], scale: float) -> list[list[TextBox]]:
    return [
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
        for row in rows
    ]


def _body_rows(scan: PageScan, rows: list[list[TextBox]]) -> list[list[TextBox]]:
    """Rows that vote on column positions: the table body, without the letterhead.

    Skipped when the page drew horizontal rules, because then the rules already
    define where the table starts and stops and there is nothing to infer.
    """
    if len(scan.y_rules) >= 3:
        return rows
    band = grid.select_table_band(rows)
    if band is None:
        return rows
    start, end = band
    return rows[start : end + 1]


def _table_rows(scan: PageScan, rows: list[list[TextBox]]) -> list[list[TextBox]]:
    """The body plus one header row above it. See ``_PageWords.table_rows``."""
    if len(scan.y_rules) >= 3:
        return rows
    band = grid.select_table_band(rows)
    if band is None:
        return rows
    start, end = band
    return rows[max(0, start - 1) : end + 1]


def _shared_boundaries(
    scans: list[PageScan], rows_by_page: dict[int, list[list[TextBox]]]
) -> list[float] | None:
    """Column boundaries in virtual coordinates, pooled across borderless pages.

    Returns ``None`` when every page draws its own vertical rules and there is
    nothing to infer.
    """
    pooled: list[list[TextBox]] = []
    for scan in scans:
        if scan.has_vertical_rules or scan.width <= 0:
            continue
        pooled.extend(
            _scaled_rows(
                _body_rows(scan, rows_by_page[scan.page_number]),
                _VIRTUAL_WIDTH / scan.width,
            )
        )

    if not pooled:
        return None

    boundaries = grid.infer_column_boundaries(pooled, _VIRTUAL_WIDTH)
    log.info(
        "inferred %d shared column(s) from %d borderless row(s)",
        max(0, len(boundaries) - 1),
        len(pooled),
    )
    return boundaries


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def extract_scanned_tables(
    pdf_path: str,
    page_indices: list[int],
    *,
    dpi: int = 300,
    sha256: str = "",
    progress: ProgressFn | None = None,
) -> list[ExtractedTable]:
    """Read every scanned page of a document into tables.

    ``page_indices`` are 0-based and come from the router — only pages that
    actually need OCR appear here, which is what keeps a 50-page document with
    4 scanned pages to four OCR passes rather than fifty.
    """
    if not page_indices:
        return []

    reason = ocr.unavailable_reason()
    if reason:
        raise RuntimeError(reason)

    import pypdfium2 as pdfium

    scans: list[PageScan] = []
    document = pdfium.PdfDocument(pdf_path)
    try:
        for position, page_index in enumerate(page_indices):
            if progress:
                progress(position, len(page_indices), f"Reading scanned page {page_index + 1}")
            scans.append(scan_page(document, page_index, dpi=dpi, sha256=sha256))
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()

    # Group into rows first: column inference needs rows to project.
    rows_by_page = {
        scan.page_number: grid.group_rows(scan.boxes, scan.y_rules) for scan in scans
    }
    shared = _shared_boundaries(scans, rows_by_page)

    tables: list[ExtractedTable] = []
    for scan in scans:
        rows = rows_by_page[scan.page_number]
        if not rows:
            log.info("page %d: no text recognised, skipping", scan.page_number)
            continue

        if scan.has_vertical_rules:
            boundaries = grid.infer_column_boundaries(
                rows, scan.width, x_rules=scan.x_rules
            )
            extractor = "ocr-grid"
        elif shared is not None and scan.width > 0:
            scale = scan.width / _VIRTUAL_WIDTH
            boundaries = [b * scale for b in shared]
            extractor = "ocr-columns"
        else:
            boundaries = grid.infer_column_boundaries(_body_rows(scan, rows), scan.width)
            extractor = "ocr-columns"

        text_grid, confidences = grid.build_grid(_table_rows(scan, rows), boundaries)
        if not text_grid:
            continue

        tables.append(
            ExtractedTable(
                page_number=scan.page_number,
                kind=PageKind.SCANNED,
                rows=text_grid,
                confidences=confidences,
                extractor=extractor,
            )
        )
        log.info(
            "page %d: rebuilt %dx%d table via %s%s",
            scan.page_number,
            len(text_grid),
            max((len(r) for r in text_grid), default=0),
            extractor,
            " (cached)" if scan.from_cache else "",
        )

    return tables
