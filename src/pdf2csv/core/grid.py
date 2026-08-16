"""Rebuilding table structure from a page image and positioned OCR text.

OCR does not read tables. RapidOCR returns text with bounding boxes and nothing
else; every row and column in the output is something this module infers. This
is the substantial work in the scanned path and it deserves the space.

The strategy is to derive row boundaries and column boundaries *independently*,
because real documents supply them independently:

============================  ==============================================
Document draws...             Boundaries come from...
============================  ==============================================
full ruled grid               horizontal rules, vertical rules
vertical rules only           whitespace rows, vertical rules  ← very common
horizontal rules only         horizontal rules, whitespace gaps
nothing (borderless)          whitespace rows, whitespace gaps
============================  ==============================================

Anchoring to ruling lines is far more reliable than inferring from spacing, so
lines win wherever they exist. Where they do not, columns are found by
projecting every text box onto the x-axis and looking for the vertical corridors
of whitespace that no row crosses.

That projection approach is used in preference to clustering the boxes' left
edges, which is the more obvious method and fails badly on finance tables:
amounts are right-aligned and labels are left-aligned, so clustering x-starts
scatters a single amount column across several phantom boundaries. A whitespace
corridor is a corridor regardless of how the text on either side is aligned.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from pdf2csv.logging_setup import get_logger
from pdf2csv.models import TextBox

log = get_logger(__name__)

# --- Tuning ---------------------------------------------------------------- #

_MIN_SKEW_DEGREES = 0.2
"""Below this, rotating costs interpolation blur and buys nothing."""

_MAX_SKEW_DEGREES = 15.0
"""Beyond this it is not skew, it is a rotated page or a failed estimate."""

_RULE_MIN_SPAN = 0.35
"""A ruling line must cross this fraction of the page to count as one."""

_MIN_GAP_FRACTION = 0.010
"""A whitespace corridor must be this wide, relative to page width, to be a
column separator. About 2.5 mm at 300 DPI — narrower than that and ordinary
inter-word spacing starts registering as a column."""

_MAX_CROSSING_FRACTION = 0.06
"""A corridor may still be a column separator if this fraction of rows cross it
— a full-width section heading should not erase every column on the page."""

_ROW_TOLERANCE = 0.55
"""Row grouping tolerance, as a multiple of median text height."""


# --------------------------------------------------------------------------- #
# Deskew
# --------------------------------------------------------------------------- #


def estimate_skew(gray: np.ndarray) -> float:
    """Estimate page tilt in degrees.

    The sign is expressed in OpenCV's rotation convention: the returned value
    is the angle to hand :func:`cv2.getRotationMatrix2D` to straighten the
    page, so a page tilted anticlockwise reports a negative angle. Callers
    should not reason about the sign — pass it straight through, or use
    :func:`deskew`, which does.

    Tries ruling lines first because they are long, straight and unambiguous.
    Falls back to the orientation of dilated text rows, which works on
    borderless documents where there is no line to measure.
    """
    import cv2

    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    min_length = max(60, gray.shape[1] // 4)
    lines = cv2.HoughLinesP(
        edges, 1, math.pi / 720, threshold=120, minLineLength=min_length, maxLineGap=20
    )

    angles: list[float] = []
    if lines is not None:
        for x0, y0, x1, y1 in lines[:, 0]:
            if x1 == x0:
                continue
            angle = math.degrees(math.atan2(float(y1 - y0), float(x1 - x0)))
            if abs(angle) <= _MAX_SKEW_DEGREES:
                angles.append(angle)

    if len(angles) >= 3:
        return float(np.median(angles))

    # No usable rules: measure the text itself.
    inverted = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, gray.shape[1] // 60), 3))
    smeared = cv2.dilate(inverted, kernel, iterations=1)
    coords = cv2.findNonZero(smeared)
    if coords is None or len(coords) < 100:
        return 0.0

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle += 90
    elif angle > 45:
        angle -= 90
    return float(angle) if abs(angle) <= _MAX_SKEW_DEGREES else 0.0


def deskew(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Straighten a page. Returns the image and the angle that was corrected.

    Deskewing before OCR is one of the cheapest accuracy wins available: a two
    degree tilt is invisible to a human and measurably degrades recognition,
    and it wrecks row grouping outright, because a row's text drifts vertically
    across the page and stops looking like one row.
    """
    import cv2

    angle = estimate_skew(gray)
    if abs(angle) < _MIN_SKEW_DEGREES or abs(angle) > _MAX_SKEW_DEGREES:
        return gray, 0.0

    height, width = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        gray,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    log.debug("deskewed by %.2f degrees", angle)
    return rotated, angle


# --------------------------------------------------------------------------- #
# Ruling lines
# --------------------------------------------------------------------------- #


def _runs_above(profile: np.ndarray, threshold: float, merge_gap: int = 4) -> list[float]:
    """Centres of contiguous runs where ``profile`` stays above ``threshold``."""
    above = profile >= threshold
    centres: list[float] = []
    start: int | None = None

    for index, flag in enumerate(above):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            centres.append((start + index - 1) / 2.0)
            start = None
    if start is not None:
        centres.append((start + len(above) - 1) / 2.0)

    if not centres:
        return []

    merged = [centres[0]]
    for centre in centres[1:]:
        if centre - merged[-1] <= merge_gap:
            merged[-1] = (merged[-1] + centre) / 2.0
        else:
            merged.append(centre)
    return merged


def detect_rules(gray: np.ndarray) -> tuple[list[float], list[float]]:
    """Find drawn horizontal and vertical rules. Returns ``(y_rules, x_rules)``.

    Morphological opening with a long thin kernel keeps only strokes that run
    for a substantial distance in one direction, which is precisely what
    distinguishes a ruling line from a row of text.
    """
    import cv2

    height, width = gray.shape[:2]
    binary = cv2.adaptiveThreshold(
        cv2.bitwise_not(gray), 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2
    )

    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, width // 40), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, height // 40)))

    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=1)

    y_rules = _runs_above(horizontal.sum(axis=1) / 255.0, _RULE_MIN_SPAN * width)
    x_rules = _runs_above(vertical.sum(axis=0) / 255.0, _RULE_MIN_SPAN * height)

    log.debug("detected %d horizontal and %d vertical rule(s)", len(y_rules), len(x_rules))
    return y_rules, x_rules


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def group_rows(boxes: list[TextBox], y_rules: list[float] | None = None) -> list[list[TextBox]]:
    """Group text boxes into visual rows.

    Uses horizontal rules when the document drew them; otherwise groups by
    vertical position with a tolerance derived from the text's own height, so
    the same code works on a dense 6pt ledger and a sparse 14pt summary.
    """
    if not boxes:
        return []

    if y_rules and len(y_rules) >= 3:
        bands: list[list[TextBox]] = [[] for _ in range(len(y_rules) - 1)]
        outside = 0

        for box in boxes:
            centre = box.y_centre
            for index in range(len(y_rules) - 1):
                if y_rules[index] <= centre < y_rules[index + 1]:
                    bands[index].append(box)
                    break
            else:
                # Text above the first rule or below the last one sits outside
                # the table the document itself drew: a letterhead, an account
                # line, a page footer. Keeping it produced junk rows in the
                # middle of a ledger — rows that then get flagged as duplicates
                # and quietly widen every column total.
                #
                # A ruled table's top border is drawn above its header, so the
                # header is inside the band and survives. A format that rules
                # only between data rows loses its header here and falls back to
                # positional column names, which is visible and recoverable;
                # fabricated transaction rows are neither.
                outside += 1

        if outside:
            log.debug("dropped %d text box(es) outside the ruled table", outside)
        return [sorted(band, key=lambda b: b.x0) for band in bands if band]

    return _group_rows_by_position(boxes)


def _group_rows_by_position(boxes: list[TextBox]) -> list[list[TextBox]]:
    heights = [b.height for b in boxes if b.height > 0]
    tolerance = (np.median(heights) if heights else 12.0) * _ROW_TOLERANCE

    ordered = sorted(boxes, key=lambda b: b.y_centre)
    rows: list[list[TextBox]] = []
    current: list[TextBox] = []
    anchor = None

    for box in ordered:
        if anchor is None or abs(box.y_centre - anchor) <= tolerance:
            current.append(box)
            # Track the running mean so a gently drifting row stays one row.
            anchor = float(np.mean([b.y_centre for b in current]))
        else:
            rows.append(sorted(current, key=lambda b: b.x0))
            current = [box]
            anchor = box.y_centre

    if current:
        rows.append(sorted(current, key=lambda b: b.x0))
    return rows


# --------------------------------------------------------------------------- #
# Columns
# --------------------------------------------------------------------------- #


def select_table_band(rows: list[list[TextBox]]) -> tuple[int, int] | None:
    """Find the run of rows that is the table body. Returns ``(start, end)``.

    A page is not only its table. A statement carries a bank name, an address
    block, an account line and a page footer, and every one of them is a run of
    text lying across the same x-range the table occupies. Letting them vote on
    where the columns are is what merges ``Date`` into ``Libellé``: three title
    lines happen to span the whitespace corridor between them, and a corridor
    that anything crosses is not a corridor.

    Body rows are identified by their *rhythm*. A table is set on a constant
    vertical pitch — that is what makes it look like a table — while titles and
    footers sit at irregular distances from everything around them. Rows whose
    spacing to a neighbour matches the page's dominant pitch are the body.

    Returns ``None`` when no clear rhythm exists (a table with wrapped rows of
    varying height, for instance), in which case the caller should fall back to
    using every row rather than trusting a guess.
    """
    if len(rows) < 5:
        return None

    centres = [float(np.mean([b.y_centre for b in row])) for row in rows]
    pitches = [centres[i + 1] - centres[i] for i in range(len(centres) - 1)]
    if not pitches:
        return None

    dominant = float(np.median(pitches))
    if dominant <= 0:
        return None

    # Tight on purpose. Rows of one table are set on identical leading, so their
    # pitch varies by a few percent at most; a letterhead three lines deep can
    # easily sit at 80% of the table's pitch and would sail through a loose
    # threshold, which is exactly the case this function exists to reject.
    tolerance = dominant * _BAND_PITCH_TOLERANCE

    def _matches(pitch: float) -> bool:
        return abs(pitch - dominant) <= tolerance

    # A row belongs to the body if it is regularly spaced from either neighbour.
    in_body = [
        (index > 0 and _matches(pitches[index - 1]))
        or (index < len(pitches) and _matches(pitches[index]))
        for index in range(len(rows))
    ]

    return _longest_run(in_body)


_BAND_PITCH_TOLERANCE = 0.18
_BAND_GAP_TOLERANCE = 1
"""How many irregular rows a body run may bridge.

One, so that a description wrapping onto a second line — which doubles the
pitch for a single row — does not cut the table in half, while a three-line
letterhead still fails to attach itself to the body.
"""


def _longest_run(flags: list[bool]) -> tuple[int, int] | None:
    """Longest run of ``True``, allowing short interruptions."""
    best: tuple[int, int] | None = None
    start: int | None = None
    last_true: int | None = None
    gap = 0

    for index, flag in enumerate(flags):
        if flag:
            if start is None:
                start = index
            last_true = index
            gap = 0
            continue
        if start is None:
            continue
        gap += 1
        if gap > _BAND_GAP_TOLERANCE:
            assert last_true is not None
            if best is None or last_true - start > best[1] - best[0]:
                best = (start, last_true)
            start = None
            last_true = None
            gap = 0

    if (
        start is not None
        and last_true is not None
        and (best is None or last_true - start > best[1] - best[0])
    ):
        best = (start, last_true)

    if best is None or (best[1] - best[0] + 1) < 3:
        return None
    return best


def infer_column_boundaries(
    rows: list[list[TextBox]],
    width: float,
    *,
    x_rules: list[float] | None = None,
) -> list[float]:
    """Find column edges. Returns boundaries including the outer two.

    Vertical rules win when the document has them. Otherwise the whitespace
    corridors are found by projection — see the module docstring for why this
    beats clustering the boxes' left edges on right-aligned money columns.
    """
    if x_rules and len(x_rules) >= 3:
        return sorted(x_rules)

    if not rows:
        return [0.0, float(width)]

    # Only rows that look tabular vote on where the columns are. A title, a
    # footer, or an address block is one or two runs of text that happen to lie
    # across the page, and letting them vote erases every corridor they touch.
    tabular = [row for row in rows if len(row) >= 2]
    if not tabular:
        tabular = rows

    span = max(1, math.ceil(width) + 1)
    coverage = np.zeros(span, dtype=np.int32)

    for row in tabular:
        occupied = np.zeros(span, dtype=bool)
        for box in row:
            start = max(0, math.floor(box.x0))
            end = min(span - 1, math.ceil(box.x1))
            if end >= start:
                occupied[start : end + 1] = True
        coverage += occupied

    n_rows = len(tabular)
    # At least one crossing is always forgiven. A single header line or a
    # merged section label should not be able to delete a column that thirty
    # data rows agree exists.
    crossing_allowance = max(1, math.floor(_MAX_CROSSING_FRACTION * n_rows))
    is_gap = coverage <= crossing_allowance
    min_gap_width = max(6, int(_MIN_GAP_FRACTION * width))

    boundaries: list[float] = []
    start: int | None = None
    for index in range(span):
        if is_gap[index] and start is None:
            start = index
        elif not is_gap[index] and start is not None:
            _consider_gap(boundaries, start, index - 1, span, min_gap_width)
            start = None
    if start is not None:
        _consider_gap(boundaries, start, span - 1, span, min_gap_width)

    left = float(min((box.x0 for row in tabular for box in row), default=0.0))
    right = float(max((box.x1 for row in tabular for box in row), default=width))
    return [left, *boundaries, right]


def _consider_gap(
    boundaries: list[float], start: int, end: int, span: int, min_width: int
) -> None:
    """Record a whitespace corridor as a column boundary, if it qualifies."""
    if end - start + 1 < min_width:
        return
    # Corridors touching either edge are page margins, not column separators.
    if start == 0 or end >= span - 1:
        return
    boundaries.append((start + end) / 2.0)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def build_grid(
    rows: list[list[TextBox]], boundaries: list[float]
) -> tuple[list[list[str]], list[list[float]]]:
    """Place every text box into its cell. Returns ``(text_grid, confidences)``.

    Boxes are assigned by their horizontal centre. Where two boxes land in the
    same cell — a wrapped description, or an amount whose currency symbol was
    detected separately — they are joined in reading order rather than one
    overwriting the other.
    """
    n_columns = max(1, len(boundaries) - 1)
    text_grid: list[list[str]] = []
    confidence_grid: list[list[float]] = []

    for row in rows:
        cells: list[list[TextBox]] = [[] for _ in range(n_columns)]
        for box in row:
            cells[_column_for(box, boundaries, n_columns)].append(box)

        text_row: list[str] = []
        confidence_row: list[float] = []
        for cell in cells:
            if not cell:
                text_row.append("")
                confidence_row.append(1.0)
                continue
            ordered = sorted(cell, key=lambda b: b.x0)
            text_row.append(" ".join(b.text for b in ordered).strip())
            confidence_row.append(min(b.confidence for b in ordered))

        text_grid.append(text_row)
        confidence_grid.append(confidence_row)

    return text_grid, confidence_grid


def _column_for(box: TextBox, boundaries: list[float], n_columns: int) -> int:
    centre = box.x_centre
    for index in range(n_columns):
        if boundaries[index] <= centre < boundaries[index + 1]:
            return index
    # Outside every boundary: clamp to the nearest edge rather than dropping it.
    return 0 if centre < boundaries[0] else n_columns - 1


def to_grayscale_array(image: Any) -> np.ndarray:
    """Coerce a PIL image or array into a 2-D uint8 array OpenCV will accept."""
    array = np.asarray(image)
    if array.ndim == 3:
        import cv2

        channels = array.shape[2]
        if channels == 4:
            array = cv2.cvtColor(array, cv2.COLOR_RGBA2GRAY)
        elif channels == 3:
            array = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        else:
            array = array[:, :, 0]
    if array.dtype != np.uint8:
        array = array.astype(np.uint8)
    return array
