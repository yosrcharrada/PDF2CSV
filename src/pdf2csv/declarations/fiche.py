"""Reading a *fiche du souscripteur* — several subscribers, one table, two pages.

A declaration is one certificate on one page. A fiche is a register: several
subscribers, one row each, and enough columns that the page cannot hold them.
Three properties follow from that, and each one breaks something that worked
for declarations.

**The table is split across pages by column, not by row.** Page one carries the
subscriber's identity, page two the instrument, and a row is the two halves at
the same position joined together. Nothing in either half says which row of the
other it belongs to; only the ordering does. That is the opposite of the
continuation handled in :mod:`pdf2csv.core.stitch`, where pages add rows to a
fixed set of columns.

**The pages are tilted.** Around three degrees, which is invisible to a reader
and fatal to row grouping: a row's text drifts about a hundred pixels down the
page while the rows themselves are only eighty apart, so a naive banding
interleaves them. Every page is deskewed before recognition.

**The recogniser merges neighbouring cells.** ``TUNISIENNE`` and
``CARTE D'IDENTITE`` come back as one box, and so do a client type and the
address beside it. Splitting the string proportionally puts the cut in the
middle of a word — ``TUNISIENNECA`` — so a merged box is re-read from its own
pixels instead, cut at the ruling line between the two cells. That is the one
place this module recognises anything twice; per-cell recognition over a whole
page is both slower and less accurate, as :mod:`pdf2csv.core.ocr` says.

The columns themselves come from the printed ruling lines rather than from the
headings. The lines are exact, they need no vocabulary, and a heading that
wraps onto two lines or merges with its neighbour cannot move them.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from pdf2csv.core import grid, ocr
from pdf2csv.declarations.mapping import (
    DeclarationFacts,
    Subscriber,
    format_amount,
)
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import TextBox

log = get_logger(__name__)

__all__ = ["looks_like_fiche", "read_fiche"]

ProgressFn = Callable[[int, int, str], None]


# --------------------------------------------------------------------------- #
# What the two halves of the table look like
# --------------------------------------------------------------------------- #

# A column is identified by the words printed above it. Matching is done on
# text with accents, case, spaces and punctuation removed, because the
# recogniser drops accents unpredictably ("Nationalite", "Quantite") and closes
# up spaces at random ("DE JOURS" but "CARTED'IDENTITE").
#
# Order matters within a half only in that `exclude` resolves the one genuine
# collision: "identifiant national" and "nature de l'identification" share a
# stem, and the longer word must not be allowed to claim the shorter column.

SUBSCRIBER_HALF = "subscriber"
INSTRUMENT_HALF = "instrument"
BOTH_HALVES = "both"
"""One page carrying the whole table rather than half of it.

The sample splits its columns over two pages because they do not fit on one,
but that is a property of this printing and not of the document class. A wider
page, or fewer columns, puts everything on a single page -- and then the rows
need no joining at all, because each one is already whole."""


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    half: str
    wants: tuple[str, ...]
    exclude: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        """Exact containment — the common case, and free."""
        if any(bad in text for bad in self.exclude):
            return False
        return any(word in text for word in self.wants)

    def similarity(self, text: str) -> float:
        """How closely the closest part of ``text`` reads as this heading.

        Recognition of a heading is not reliable enough to insist on an exact
        match. Real failures seen on these scans include a heading clipped
        where two columns were run together, an ``I`` read for an ``l`` in
        *Interet*, and dropped accents. Any one of them silently loses a
        column, and a lost date column is not a visible failure -- it is a row
        with a plausible wrong date in it.

        Scored by sliding the expected word across the recognised text, so a
        heading that is only part of a longer run still matches.
        """
        if any(bad in text for bad in self.exclude):
            return 0.0
        best = 0.0
        for word in self.wants:
            if word in text:
                return 1.0
            span = len(word)
            if len(text) < span:
                best = max(best, SequenceMatcher(None, word, text).ratio())
                continue
            for start in range(len(text) - span + 1):
                window = text[start : start + span]
                best = max(best, SequenceMatcher(None, word, window).ratio())
        return best


COLUMN_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec("subscriber_name", SUBSCRIBER_HALF, ("souscripteur", "nomdu")),
    ColumnSpec("nationality", SUBSCRIBER_HALF, ("nationalite", "nationalit")),
    ColumnSpec(
        "nature_of_identification",
        SUBSCRIBER_HALF,
        ("identification", "naturede"),
    ),
    ColumnSpec(
        "national_id",
        SUBSCRIBER_HALF,
        ("identifiant", "national"),
        exclude=("identification", "naturede"),
    ),
    ColumnSpec("client_type", SUBSCRIBER_HALF, ("typeduclient", "typedu")),
    ColumnSpec("address", SUBSCRIBER_HALF, ("adresse",)),
    ColumnSpec("restriction", SUBSCRIBER_HALF, ("restriction",)),
    ColumnSpec("libelle", INSTRUMENT_HALF, ("libelle", "certificatde")),
    ColumnSpec("taux", INSTRUMENT_HALF, ("taux",)),
    ColumnSpec("prix_unitaire", INSTRUMENT_HALF, ("prixunitaire", "unitaire")),
    ColumnSpec(
        "montant",
        INSTRUMENT_HALF,
        ("montant",),
        exclude=("montantnet", "netducd", "ducd"),
    ),
    ColumnSpec("quantite", INSTRUMENT_HALF, ("quantite", "quantit")),
    ColumnSpec("date_souscription", INSTRUMENT_HALF, ("souscription",)),
    ColumnSpec("date_remboursement", INSTRUMENT_HALF, ("remboursement",)),
    ColumnSpec("days", INSTRUMENT_HALF, ("nombre", "jours")),
    ColumnSpec(
        "interet_brut", INSTRUMENT_HALF, ("interetbrut", "interetbru"), exclude=()
    ),
    ColumnSpec("retenue", INSTRUMENT_HALF, ("retenue", "lasource", "source")),
    ColumnSpec("interet_net", INSTRUMENT_HALF, ("interetnet",)),
    ColumnSpec("montant_net", INSTRUMENT_HALF, ("montantnet", "netducd", "ducd")),
)

_IDENTITY_KEYS = tuple(
    spec.key for spec in COLUMN_SPECS if spec.half == SUBSCRIBER_HALF
)

_REQUIRED = {
    SUBSCRIBER_HALF: ("subscriber_name", "client_type"),
    INSTRUMENT_HALF: ("libelle", "taux", "date_souscription", "date_remboursement"),
}

_MARKERS = ("FICHE", "SOUSCRIPTEUR", "BILLET DE TRESORERIE")

# Ink darker than this counts as printed. Chosen well above the paper's grey so
# a scan's background never registers, and well below any glyph.
_INK = 160
# A column of the table band that is ink for this fraction of its height is a
# printed ruling line rather than a run of glyphs.
_RULE_FILL = 0.85
# A box has to lie this far into a neighbouring column before it counts as
# spanning two cells and gets re-read. Below it, a slight overhang is just the
# recogniser's box being generous.
_STRADDLE = 0.18
# A y-cluster has to occupy this share of the table's columns to be a row of
# its own. Anything narrower is the first line of a cell that wrapped.
_ROW_COVERAGE = 0.40
# How close a misrecognised heading has to read before it is accepted. High
# enough that "remboursement" never answers for "souscription", low enough to
# absorb a wrong letter or a clipped ending.
_FUZZY = 0.82
# Enlargement applied when a cell is re-read on its own. Two is enough to
# recover the spacing between printed capitals; three gains nothing.
_MAGNIFY = 2
# Columns whose value is prose rather than a figure, and so are always re-read.
# A digit is recognised correctly either way, but a name or a client type is
# written into the CSV with its spacing visible.
_PROSE = frozenset(
    {
        "subscriber_name",
        "client_type",
        "nationality",
        "nature_of_identification",
        # Both of these are written into the CSV as the document states them,
        # so their word spacing is visible to whoever reads it. The libelle in
        # particular is the document's own name for the instrument -- closed up
        # to "SER BTKL 8.40% CD 31072026" it is a good deal harder to check against
        # the paper than "SER BTKL 8.40% CD 31072026".
        "libelle",
        "address",
    }
)

_DATE = re.compile(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})")
_DRAWN = re.compile(r"(?:FAITA|FAIT|LE)", re.I)


def _fold(text: str) -> str:
    """Accent-free, case-free, punctuation-free — the form headings match in."""
    stripped = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", stripped.lower())


# --------------------------------------------------------------------------- #
# One page, reduced to a grid
# --------------------------------------------------------------------------- #


@dataclass
class PageTable:
    """The table on one page: which columns it holds and what is in them."""

    half: str
    columns: dict[str, tuple[float, float]]
    rows: list[dict[str, str]]
    confidence: float = 1.0
    document_date: dt.date | None = None
    letterhead: str = ""

    @property
    def height(self) -> int:
        return len(self.rows)


def _runs(flags: Sequence[bool]) -> list[tuple[int, int]]:
    """Maximal runs of ``True``, as inclusive index pairs."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            out.append((start, index - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _table_band(dark: Any) -> tuple[int, int, int, int] | None:
    """Locate the table: the horizontal band of ink that spans the most columns.

    A fiche page carries a letterhead, a table and a signature block. The table
    is the only one of the three that reaches across the page, so width — not
    height and not position — is what identifies it, and no page furniture has
    to be described in advance.
    """
    import numpy as np

    row_ink = dark.sum(axis=1)
    bands = [pair for pair in _runs([bool(v > 3) for v in row_ink]) if pair[1] - pair[0] > 8]
    if not bands:
        return None

    def spread(pair: tuple[int, int]) -> int:
        return int(dark[pair[0] : pair[1] + 1].sum(axis=0).astype(bool).sum())

    top, bottom = max(bands, key=spread)
    columns = np.flatnonzero(dark[top : bottom + 1].sum(axis=0) > 0)
    if columns.size < 50:
        return None
    return top, bottom, int(columns[0]), int(columns[-1])


def _column_edges(dark: Any, band: tuple[int, int, int, int]) -> list[float]:
    """Column boundaries, taken from the printed vertical rules.

    Far steadier than inferring them from the headings: a rule is a single
    unambiguous x, whereas a heading can wrap onto two lines, merge with its
    neighbour, or sit anywhere within its cell.
    """
    top, bottom, left, right = band
    sub = dark[top : bottom + 1]
    height = bottom - top + 1
    ink = sub.sum(axis=0)

    edges = [float(left)]
    for start, end in _runs([bool(v >= _RULE_FILL * height) for v in ink]):
        centre = (start + end) / 2.0
        if left < centre < right:
            edges.append(centre)
    edges.append(float(right))
    return sorted(set(edges))


def _identify(
    edges: list[float], header_boxes: Iterable[TextBox]
) -> dict[int, str]:
    """Name each column from the words printed above it."""
    text_by_column: dict[int, list[str]] = {}
    for box in header_boxes:
        spanned = _columns_spanned(box, edges)
        if len(spanned) == 1:
            text_by_column.setdefault(spanned[0], []).append(box.text)
            continue
        for index, word in _spread(box, edges):
            text_by_column.setdefault(index, []).append(word)

    # Ascending column order, so that a heading naming two columns in the order
    # they are printed — "souscription remboursement" — assigns the first word
    # to the first column. Iterating the dictionary in box order instead made
    # the two date columns swap whenever the recogniser emitted them merged,
    # which produced a maturity before its own subscription.
    folded_by_column = {
        index: _fold(" ".join(chunks)) for index, chunks in text_by_column.items()
    }

    # Exact matches first, across every column, before any approximate one is
    # considered. Otherwise a column whose heading was read perfectly could be
    # claimed by a near-miss from its neighbour.
    named: dict[int, str] = {}
    for index in sorted(folded_by_column):
        for spec in COLUMN_SPECS:
            if spec.key in named.values():
                continue
            if spec.matches(folded_by_column[index]):
                named[index] = spec.key
                break

    for index in sorted(folded_by_column):
        if index in named:
            continue
        taken = set(named.values())
        scored = [
            (spec.similarity(folded_by_column[index]), spec.key)
            for spec in COLUMN_SPECS
            if spec.key not in taken
        ]
        best = max(scored, default=(0.0, ""))
        if best[0] >= _FUZZY:
            log.info(
                "column %d read as %r at %.0f%% confidence (heading recognised as %r)",
                index,
                best[1],
                best[0] * 100,
                folded_by_column[index],
            )
            named[index] = best[1]
    return named


def _spread(box: TextBox, edges: list[float]) -> list[tuple[int, str]]:
    """Give each word of a merged heading to the column it is printed over.

    Estimating where each *word* falls and keeping it whole, rather than
    cutting the string at the ruling line: a proportional cut of
    ``"souscription remboursement"`` lands one character early and yields
    ``"souscriptio"``, which matches no heading at all. Words are the natural
    unit here because a heading spanning two columns is always two headings
    printed side by side.
    """
    width = max(box.x1 - box.x0, 1.0)
    length = max(len(box.text), 1)
    out: list[tuple[int, str]] = []
    offset = 0
    for word in box.text.split():
        start = box.text.index(word, offset)
        offset = start + len(word)
        centre = box.x0 + width * (start + len(word) / 2.0) / length
        for index in range(len(edges) - 1):
            if edges[index] <= centre < edges[index + 1]:
                out.append((index, word))
                break
    return out


def _columns_spanned(box: TextBox, edges: list[float]) -> list[int]:
    """Which cells a box occupies, ignoring a slight overhang into the next."""
    spanned: list[int] = []
    for index in range(len(edges) - 1):
        low, high = edges[index], edges[index + 1]
        overlap = min(box.x1, high) - max(box.x0, low)
        if overlap <= 0:
            continue
        width = max(box.x1 - box.x0, 1.0)
        if overlap >= _STRADDLE * min(width, high - low):
            spanned.append(index)
    if not spanned:
        centre = box.x_centre
        for index in range(len(edges) - 1):
            if edges[index] <= centre < edges[index + 1]:
                return [index]
        return [0 if centre < edges[0] else len(edges) - 2]
    return spanned


def _cluster(boxes: list[TextBox]) -> list[list[TextBox]]:
    """Group boxes into printed lines by vertical position."""
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: b.y_centre)
    heights = sorted(b.y1 - b.y0 for b in ordered)
    tolerance = max(heights[len(heights) // 2] * 0.6, 4.0)

    groups: list[list[TextBox]] = [[ordered[0]]]
    for box in ordered[1:]:
        if box.y_centre - groups[-1][-1].y_centre <= tolerance:
            groups[-1].append(box)
        else:
            groups.append([box])
    return groups


def _row_bands(
    body: list[TextBox], edges: list[float]
) -> list[tuple[float, float]]:
    """Split the body into rows, absorbing cells that wrapped onto two lines.

    A wrapped cell puts its first line above the row everything else sits on,
    so a plain line-grouping produces more groups than there are rows. What
    separates a real row from a stray first line is *width*: a row has content
    in most of the table's columns, a wrapped fragment in one or a few.

    Fragments are folded into the row *below* them, which is where the rest of
    their own cell is — the last line of a wrapped cell is the one that sits on
    the row's baseline.
    """
    columns = max(len(edges) - 1, 1)
    groups = _cluster(body)
    if not groups:
        return []

    def covers(group: list[TextBox]) -> int:
        seen: set[int] = set()
        for box in group:
            seen.update(_columns_spanned(box, edges))
        return len(seen)

    is_row = [covers(group) >= max(2, _ROW_COVERAGE * columns) for group in groups]
    if not any(is_row):
        # Nothing looks wide enough — a one-column table, or a single row.
        is_row = [True] * len(groups)

    bands: list[tuple[float, float]] = []
    pending_top: float | None = None
    for group, row in zip(groups, is_row, strict=True):
        top = min(b.y0 for b in group)
        bottom = max(b.y1 for b in group)
        if row:
            bands.append((pending_top if pending_top is not None else top, bottom))
            pending_top = None
        elif pending_top is None:
            pending_top = top

    if pending_top is not None and bands:
        # Trailing fragment with no row beneath it: give it to the last row.
        last_top, last_bottom = bands[-1]
        bands[-1] = (last_top, max(last_bottom, pending_top))
    return bands


def _reread(image: Any, y0: float, y1: float, x0: float, x1: float) -> str:
    """Recognise one cell from its own pixels, enlarged.

    Two different problems need this, and the magnification is what solves both.

    A cell the recogniser ran together with its neighbour cannot be split by
    cutting the string: character widths vary and the gap between columns holds
    no glyphs, so a proportional cut lands mid-word — ``TUNISIENNECA``. Cutting
    the *image* at the ruling line cannot make that mistake.

    Separately, the recogniser closes up the spaces in printed capitals, so a
    name arrives as ``MARIEDUPONTMARTIN`` and a client type as
    ``PERSONNEMORALE``. At twice the size it reads them as written. That is the
    opposite of the warning in :mod:`pdf2csv.core.ocr` against per-cell
    recognition, and deliberately so: the warning is about losing surrounding
    context on a whole page of cells, while this is a handful of identity cells
    where the word spacing is visible in the output and the enlargement buys
    back more than the lost context costs.
    """
    import cv2

    top = max(int(y0) - 4, 0)
    bottom = min(int(y1) + 4, image.shape[0])
    left = max(int(x0) + 2, 0)
    right = min(int(x1) - 2, image.shape[1])
    if bottom - top < 6 or right - left < 6:
        return ""

    crop = image[top:bottom, left:right]
    crop = cv2.resize(crop, None, fx=_MAGNIFY, fy=_MAGNIFY, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(crop, 14, 14, 14, 14, cv2.BORDER_REPLICATE)
    found = ocr.recognise(padded)
    # Group into printed lines before ordering. Sorting on the raw y alone
    # interleaves a wrapped value, because two boxes on one line rarely share a
    # centre to the pixel and the sort then reads across the wrap.
    lines = _cluster(found)
    return " ".join(
        box.text
        for line in lines
        for box in sorted(line, key=lambda b: b.x0)
    ).strip()


def read_page_table(image: Any, boxes: list[TextBox]) -> PageTable | None:
    """Reduce one deskewed page to named columns and rows of text."""
    dark = image < _INK
    band = _table_band(dark)
    if band is None:
        return None
    top, bottom, _, _ = band

    edges = _column_edges(dark, band)
    if len(edges) < 3:
        return None

    inside = [b for b in boxes if top - 4 <= b.y_centre <= bottom + 4]
    groups = _cluster(inside)
    if not groups:
        return None

    # The heading occupies the lines above the first row of values. A value
    # line is recognised by carrying a date or an amount; a heading never does.
    first_value = next(
        (i for i, group in enumerate(groups) if _looks_like_values(group)),
        None,
    )
    if first_value in (None, 0):
        return None

    # A cell in the first row that wrapped puts its opening line above the rest
    # of that row, between the heading and the first line carrying a figure.
    # Left alone it is read as part of the heading and its text is lost, which
    # is how "PERSONNE PHYSIQUE" became "PHYSIQUE" for the first subscriber
    # only. Anything above the first value line that names no column belongs to
    # the body.
    while first_value > 1 and not _names_a_column(groups[first_value - 1], edges):
        first_value -= 1

    header_boxes = [b for group in groups[:first_value] for b in group]
    body = [b for group in groups[first_value:] for b in group]

    named = _identify(edges, header_boxes)
    if not named:
        return None
    half = _half_of(named)
    if half is None:
        return None

    rows: list[dict[str, str]] = []
    lowest = 1.0
    for row_top, row_bottom in _row_bands(body, edges):
        in_row = [b for b in body if row_top - 2 <= b.y_centre <= row_bottom + 2]
        parts: dict[int, list[tuple[float, float, str]]] = {}
        merged: set[int] = set()
        for box in in_row:
            lowest = min(lowest, box.confidence)
            spanned = _columns_spanned(box, edges)
            for index in spanned:
                parts.setdefault(index, []).append((box.y_centre, box.x0, box.text))
                if len(spanned) > 1:
                    merged.add(index)

        row: dict[str, str] = {}
        for index, found in parts.items():
            key = named.get(index)
            if key is None:
                continue
            if index in merged or key in _PROSE:
                # Either the recogniser ran this cell together with its
                # neighbour -- in which case the boxes here belong to both and
                # cannot be used -- or it is a cell whose word spacing matters.
                text = _reread(
                    image, row_top, row_bottom, edges[index], edges[index + 1]
                )
            else:
                text = " ".join(t for _, _, t in sorted(found))
            row[key] = text.strip()

        if any(row.values()):
            rows.append(row)

    if not rows:
        return None

    return PageTable(
        half=half,
        columns={
            named[i]: (edges[i], edges[i + 1]) for i in named if i + 1 < len(edges)
        },
        rows=rows,
        confidence=lowest,
        document_date=_drawn_date(boxes, bottom),
        letterhead=" ".join(b.text for b in boxes if b.y_centre < top),
    )


def _names_a_column(group: list[TextBox], edges: list[float]) -> bool:
    """Does this line carry heading words, rather than a stray wrapped value?"""
    folded = _fold(" ".join(b.text for b in group))
    return any(spec.similarity(folded) >= _FUZZY for spec in COLUMN_SPECS)


def _looks_like_values(group: list[TextBox]) -> bool:
    """A line of data carries a date or a run of digits; a heading does not."""
    joined = " ".join(b.text for b in group)
    if _DATE.search(joined):
        return True
    digits = sum(c.isdigit() for c in joined)
    return digits >= 6


def _half_of(named: dict[int, str]) -> str | None:
    """Which half of the table this page holds, if it holds a usable one."""
    keys = set(named.values())
    has_identity = all(key in keys for key in _REQUIRED[SUBSCRIBER_HALF])
    has_instrument = all(key in keys for key in _REQUIRED[INSTRUMENT_HALF])
    if has_identity and has_instrument:
        return BOTH_HALVES
    if has_instrument:
        return INSTRUMENT_HALF
    if has_identity:
        return SUBSCRIBER_HALF
    return None


def _drawn_date(boxes: Iterable[TextBox], table_bottom: float) -> dt.date | None:
    """The date the document itself is dated — ``Fait a Tunis Le 03/08/2026``.

    Looked for below the table, where the signature block sits, so a date
    inside the table can never be mistaken for it.
    """
    for box in sorted(boxes, key=lambda b: b.y_centre):
        if box.y_centre <= table_bottom:
            continue
        if not _DRAWN.search(_fold(box.text).upper()):
            continue
        match = _DATE.search(box.text)
        if match:
            day, month, year = (int(g) for g in match.groups())
            try:
                return dt.date(year, month, day)
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


def looks_like_fiche(text: str) -> bool:
    """Cheap test against a page's text layer, where there is one."""
    upper = text.upper()
    return any(marker in upper for marker in _MARKERS)


def _prepare(document: Any, index: int, dpi: int) -> PageTable | None:
    """Render one page the right way up and straight, and recognise it.

    Orientation is resolved by trying each quarter turn and keeping the one
    that yields a readable table, for the same reason the declaration reader
    does it: these pages are landscape content on a portrait page, `rotation`
    reads zero on every one of them, and the recogniser silently corrects
    upside-down text, so no property of the image distinguishes the two
    candidates. Whether a table falls out of it does.
    """
    import cv2

    from pdf2csv.declarations.facts import _render

    page = _render(document, index, dpi)
    for rotation, turned in (
        (270, cv2.rotate(page, cv2.ROTATE_90_COUNTERCLOCKWISE)),
        (90, cv2.rotate(page, cv2.ROTATE_90_CLOCKWISE)),
        (0, page),
        (180, cv2.rotate(page, cv2.ROTATE_180)),
    ):
        straight, angle = grid.deskew(turned)
        boxes = ocr.recognise(straight)
        if len(boxes) < 6:
            continue
        table = read_page_table(straight, boxes)
        if table is not None:
            log.info(
                "fiche page %d: read at rotation %d, deskewed %.2f deg, %d row(s) of %s",
                index + 1,
                rotation,
                angle,
                table.height,
                table.half,
            )
            return table
    return None


def read_fiche(
    pdf_path: Any,
    *,
    dpi: int = 200,
    progress: ProgressFn | None = None,
) -> list[DeclarationFacts]:
    """Read every subscriber row in a fiche, or return an empty list.

    An empty list means "not a fiche", never "a fiche that went wrong" — a
    document this cannot understand falls back to the declaration reader and
    then to ordinary table extraction, which is how one upload box serves all
    three.
    """
    import pypdfium2

    document = pypdfium2.PdfDocument(str(pdf_path))
    try:
        total = len(document)
        tables: list[PageTable] = []
        for index in range(total):
            if progress:
                progress(index, total, f"Reading page {index + 1} of {total}")
            table = _prepare(document, index, dpi)
            if table is not None:
                tables.append(table)
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()

    if not tables:
        return []
    facts = _join(tables)
    for one in facts:
        one.page_count = total
    return facts


def _join(tables: list[PageTable]) -> list[DeclarationFacts]:
    """Join the halves of the table and turn each row into facts.

    The halves are matched by position, because that is the only thing that
    relates them: neither half repeats a key, and neither names the other. A
    mismatch in height is therefore unrecoverable rather than merely awkward —
    pairing a subscriber with the wrong instrument would produce a row that
    looks entirely ordinary and is wrong about who bought what — so it is
    refused outright.
    """
    carriers = [t for t in tables if t.half in (INSTRUMENT_HALF, BOTH_HALVES)]
    if not carriers:
        return []

    rows: list[dict[str, str]] = []
    for table in carriers:
        rows.extend(table.rows)

    # Only pages holding the identity half on their own need pairing. A page
    # carrying the whole table already has each subscriber beside the
    # instrument they bought, and nothing has to be inferred from ordering.
    identities: list[dict[str, str]] = []
    for table in tables:
        if table.half == SUBSCRIBER_HALF:
            identities.extend(table.rows)

    if identities and len(identities) != len(rows):
        log.warning(
            "fiche halves disagree: %d subscriber row(s) against %d instrument row(s); "
            "the identity columns are left empty rather than paired by guesswork",
            len(identities),
            len(rows),
        )
        identities = []

    drawn = next((t.document_date for t in tables if t.document_date), None)
    confidence = min((t.confidence for t in tables), default=1.0)
    letterhead = next((t.letterhead for t in tables if t.letterhead.strip()), "")
    page = next(
        (i for i, t in enumerate(tables, 1) if t.half in (INSTRUMENT_HALF, BOTH_HALVES)),
        1,
    )

    facts: list[DeclarationFacts] = []
    for index, row in enumerate(rows):
        # A whole-table page carries the identity in the row itself; a split
        # one supplies it from the other half, matched by position.
        identity = {key: row[key] for key in _IDENTITY_KEYS if row.get(key)}
        if not identity and index < len(identities):
            identity = identities[index]
        built = _facts_from_row(
            row, identity, drawn, letterhead, confidence, page, index
        )
        if built is not None:
            facts.append(built)
    return facts


# --------------------------------------------------------------------------- #
# One row of the table, as facts
# --------------------------------------------------------------------------- #

_PERCENT = re.compile(r"(\d{1,3}(?:[.,]\d{1,4})?)\s*%")


def _number(text: str | None) -> float | None:
    """A printed amount. ``'500 000.000'`` and ``'1 000 000,000'`` both count."""
    if not text:
        return None
    from pdf2csv.core.amounts import parse_amount

    return parse_amount(text)


def _percent(text: str | None) -> float | None:
    """``'8.40%'`` → ``8.4``. Percent, matching :class:`DeclarationFacts`."""
    if not text:
        return None
    match = _PERCENT.search(text.replace(" ", ""))
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def _whole(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def _date_in(text: str | None) -> dt.date | None:
    if not text:
        return None
    match = _DATE.search(text)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _facts_from_row(
    row: dict[str, str],
    identity: dict[str, str],
    drawn: dt.date | None,
    letterhead: str,
    confidence: float,
    page: int,
    index: int,
) -> DeclarationFacts | None:
    """Turn one joined row into the facts the mapping consumes.

    The libellé is used as the title, because it carries the issuer's own
    abbreviation — ``SER BTKL 8.40% CD 31072026``. The letterhead is the
    fallback for a fiche whose libellé is unreadable; it names the company in
    full, which resolves to the same issuer.
    """
    libelle = (row.get("libelle") or "").strip()
    title = libelle or letterhead

    taux = _percent(row.get("taux")) or _percent(libelle)
    souscription = _date_in(row.get("date_souscription"))
    remboursement = _date_in(row.get("date_remboursement"))
    montant = _number(row.get("montant"))
    prix = _number(row.get("prix_unitaire"))
    quantite = _whole(row.get("quantite"))

    if quantite is None and montant and prix:
        quantite = round(montant / prix)

    missing = [
        name
        for name, value in (
            ("taux", taux),
            ("date de souscription", souscription),
            ("date de remboursement", remboursement),
            ("quantite", quantite),
        )
        if value is None
    ]
    if missing:
        log.warning(
            "fiche row %d skipped: could not read %s", index + 1, ", ".join(missing)
        )
        return None

    subscriber = None
    if any((identity.get(k) or "").strip() for k in identity):
        subscriber = Subscriber(
            name=_tidy(identity.get("subscriber_name")),
            client_type=_tidy(identity.get("client_type")),
            nationality=_tidy(identity.get("nationality")),
            nature_of_identification=_tidy(identity.get("nature_of_identification")),
            national_id=(identity.get("national_id") or "").strip(),
            address=_tidy(identity.get("address")),
        )

    return DeclarationFacts(
        title=title,
        taux=float(taux),
        quantite=int(quantite),
        date_souscription=souscription,
        date_remboursement=remboursement,
        prix_unitaire=prix,
        montant=montant,
        document_date=drawn,
        subscriber=subscriber,
        extras=_source_values(row, identity),
        source_page=page,
        libelle=libelle,
        confidence=confidence,
    )


# Columns a fiche prints that nothing in the standard layout derives, mapped
# onto the headings they are exported under.
_CARRIED = {
    "days": "Nombre de jours",
    "interet_brut": "Interet brut",
    "retenue": "Retenue a la source",
    "interet_net": "Interet net",
    "montant_net": "Montant net",
}
_CARRIED_IDENTITY = {
    "address": "Adresse",
    "restriction": "Restriction",
}


def _source_values(row: dict[str, str], identity: dict[str, str]) -> dict[str, str]:
    """The fiche's own columns, kept as the document stated them.

    The interest figures are re-punctuated into the comma-decimal convention
    the rest of the file is written in, because the recogniser returns them
    with a full stop and the file is read in a locale where that is a
    different number. A value that will not parse is carried through
    unchanged rather than dropped -- an odd-looking cell is answerable, a
    missing one is not.
    """
    carried: dict[str, str] = {}
    for key, heading in _CARRIED.items():
        text = (row.get(key) or "").strip()
        if not text:
            continue
        amount = _number(text)
        carried[heading] = format_amount(amount) if amount is not None else text

    for key, heading in _CARRIED_IDENTITY.items():
        text = (identity.get(key) or "").strip()
        if text:
            carried[heading] = _tidy(text)
    return carried


def _tidy(text: str | None) -> str:
    """Collapse whitespace. The spacing itself comes from :func:`_reread`."""
    return re.sub(r"\s+", " ", text).strip() if text else ""
