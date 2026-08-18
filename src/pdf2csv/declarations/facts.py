"""Reading the five facts off a scanned declaration.

These documents are landscape content stored on a portrait page, so every page
arrives rotated a quarter turn with nothing in the PDF metadata to say so —
``page.rotation`` reads 0. Worse, the direction is not consistent: in one real
two-page document, page 1 is rotated one way and page 2 the other.

**Orientation is resolved by trying and checking, not by a heuristic.** Each
plausible rotation is recognised and then *parsed*; whichever yields a complete,
self-consistent set of facts is the right one. Measures like "which way up is
the text" or "where is the centre of mass" were tried against the real
documents and both picked the wrong answer on at least one page, because the
recogniser corrects upside-down text by itself and the two candidates then look
almost identical. Parsing is the only test that cannot be fooled: a page read
the wrong way round does not produce a date under the *Date de souscription*
heading.

Extraction is anchored on the printed headings rather than on grid geometry.
The layout is fixed and the headings are known, so finding a heading and reading
the cell beneath it beats reconstructing a table — especially on a scan where a
ruling line may not survive. Three things about real scans make that harder than
it sounds, and each is handled explicitly below: headings wrap onto two lines,
adjacent headings merge into a single recognised box, and a value can arrive
glued to its neighbour with no separator.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pdf2csv.core import ocr
from pdf2csv.core.amounts import parse_amount, parse_date
from pdf2csv.declarations.mapping import DeclarationFacts
from pdf2csv.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["PageReading", "extract_declarations", "extract_from_boxes"]

DEFAULT_DPI = 200
"""Enough here: the facts are a rate, an integer and two dates in a large bold
face. 300 buys nothing on these documents and doubles the time."""

_WIDE_BOX_MIN = 0.60
"""Fraction of boxes wider than tall for an orientation to be worth parsing."""


# --------------------------------------------------------------------------- #
# Text folding
# --------------------------------------------------------------------------- #


def _fold(text: str) -> str:
    """Accent-, case- and space-insensitive key for matching headings.

    OCR renders ``Libellé`` as ``Libelle``, ``Quantité`` as ``Quantite`` and
    drops spaces unpredictably. Folding all of that away matches what the
    recogniser produces rather than what the document says.
    """
    stripped = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", stripped.casefold())


# Heading → the fact beneath it, longest first so that "prixunitaire" is
# matched before "prix" would be, and "datedesouscription" before "datede".
_ANCHORS: dict[str, tuple[str, ...]] = {
    "libelle": ("libelleducertificat", "libelledu"),
    "prix_unitaire": ("prixunitaire",),
    "date_souscription": ("datedesouscription", "datesouscription"),
    "date_remboursement": ("datederemboursement", "dateremboursement"),
    "montant": ("montant",),
    "quantite": ("quantite",),
    "taux": ("taux",),
}

# No trailing word boundary: the recogniser routinely returns the title with
# the spaces closed up, as "DECLARATIONCIL49-2026".
_TITLE_PATTERN = re.compile(r"DECLARATION", re.IGNORECASE)

# Deliberately without a trailing \b. On a real document the rate is glued to
# the date with no separator — "31/07/20268,00%" — and a word boundary after
# the year refuses to match it at all.
_DATE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})")

# A rate is at most two digits, and must not be the tail of a longer number:
# without the lookbehind, "31/07/20268,00%" yields 268,00.
_PERCENT = re.compile(r"(?<!\d)(\d{1,2}(?:[.,]\d{1,4})?)\s*%")

_INT = re.compile(r"(?<!\d)(\d{1,9})(?!\d)")


@dataclass
class PageReading:
    """One page, recognised at a particular rotation."""

    page_number: int
    rotation: int
    boxes: list[Any] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0

    @property
    def wide_fraction(self) -> float:
        if not self.boxes:
            return 0.0
        wide = sum(1 for b in self.boxes if (b.x1 - b.x0) > (b.y1 - b.y0))
        return wide / len(self.boxes)

    def text(self) -> str:
        return " ".join(b.text for b in self.boxes)


@dataclass
class Anchor:
    """A heading, reduced to the column it labels."""

    key: str
    x0: float
    x1: float
    bottom: float
    text: str


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Shared width as a fraction of the narrower span."""
    left, right = max(a0, b0), min(a1, b1)
    if right <= left:
        return 0.0
    narrower = min(a1 - a0, b1 - b0)
    return (right - left) / narrower if narrower > 0 else 0.0


def _median_height(boxes: list[Any]) -> float:
    heights = sorted((b.y1 - b.y0) for b in boxes if b.y1 > b.y0)
    return heights[len(heights) // 2] if heights else 20.0


def _lines(boxes: list[Any]) -> list[list[Any]]:
    """Group boxes into visual lines by vertical position."""
    if not boxes:
        return []
    tolerance = _median_height(boxes) * 0.6
    ordered = sorted(boxes, key=lambda b: b.y_centre)
    lines: list[list[Any]] = [[ordered[0]]]
    for box in ordered[1:]:
        if abs(box.y_centre - lines[-1][-1].y_centre) <= tolerance:
            lines[-1].append(box)
        else:
            lines.append([box])
    return [sorted(line, key=lambda b: b.x0) for line in lines]


# --------------------------------------------------------------------------- #
# Headings
# --------------------------------------------------------------------------- #


def _heading_candidates(boxes: list[Any]) -> list[tuple[str, float, float, float]]:
    """Every box, plus every box joined to the one below it in the same column.

    ``Date de`` and ``souscription`` are recognised as two boxes on two lines.
    Neither matches a heading on its own; joined, they do. Each candidate is
    ``(text, x0, x1, bottom)``.
    """
    lines = _lines(boxes)
    height = _median_height(boxes)
    candidates: list[tuple[str, float, float, float]] = []

    for index, line in enumerate(lines):
        for box in line:
            candidates.append((box.text, box.x0, box.x1, box.y1))

            if index + 1 >= len(lines):
                continue
            for below in lines[index + 1]:
                gap = below.y0 - box.y1
                if gap > height * 1.4 or gap < -height * 0.5:
                    continue
                if _overlap(box.x0, box.x1, below.x0, below.x1) < 0.30:
                    continue
                candidates.append(
                    (
                        f"{box.text} {below.text}",
                        min(box.x0, below.x0),
                        max(box.x1, below.x1),
                        below.y1,
                    )
                )
    return candidates


def build_anchors(boxes: list[Any]) -> dict[str, Anchor]:
    """Locate each known heading and the column it covers.

    Where one recognised box carries two headings — ``Taux Prix unitaire`` is a
    single box on the reference document — its width is divided between them in
    proportion to where each word sits in the text. Without that split, both
    headings claim the whole span and each picks up whichever value happens to
    sit a pixel higher, so the rate and the unit price get swapped.
    """
    found: dict[str, Anchor] = {}
    _scores: dict[str, float] = {}

    for text, x0, x1, bottom in _heading_candidates(boxes):
        folded = _fold(text)
        if not folded:
            continue

        hits: list[tuple[int, int, str]] = []
        for key, needles in _ANCHORS.items():
            for needle in needles:
                position = folded.find(needle)
                if position >= 0:
                    hits.append((position, position + len(needle), key))
                    break

        if not hits:
            continue

        hits.sort()
        width = x1 - x0
        span = len(folded)

        for start, end, key in hits:
            if len(hits) > 1 and span > 0:
                # Proportional slice of the box for this word.
                sub0 = x0 + width * (start / span)
                sub1 = x0 + width * (end / span)
            else:
                sub0, sub1 = x0, x1

            # Coverage: how much of this candidate's text is the heading itself.
            # Scoring by narrowness instead looks reasonable and is wrong -- it
            # prefers a heading joined to the value beneath it, because the
            # proportional slice of a longer string is narrower. That anchor
            # then sits on the value row and reads the *next* row down, which is
            # how prix unitaire silently picked up the montant.
            coverage = (end - start) / span if span else 0.0

            existing = _scores.get(key)
            if existing is None or coverage > existing:
                _scores[key] = coverage
                found[key] = Anchor(key=key, x0=sub0, x1=sub1, bottom=bottom, text=text)

    return found


def _value_below(anchor: Anchor, boxes: list[Any], *, max_gap: float) -> Any | None:
    """The box sitting under ``anchor`` in the same column."""
    candidates = [
        b
        for b in boxes
        if b.y0 >= anchor.bottom - 2
        and b.y0 - anchor.bottom <= max_gap
        and _overlap(anchor.x0, anchor.x1, b.x0, b.x1) >= 0.30
    ]
    if not candidates:
        return None
    # Nearest row first; within a row, the best column match.
    return min(
        candidates,
        key=lambda b: (b.y0, -_overlap(anchor.x0, anchor.x1, b.x0, b.x1)),
    )


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #


def _strip_dates(text: str) -> str:
    return _DATE.sub(" ", text or "")


def _first_percent(text: str) -> float | None:
    """The rate, ignoring any date glued to it."""
    match = _PERCENT.search(_strip_dates(text))
    return parse_amount(match.group(1), decimal_sep=",") if match else None


def _first_date(text: str) -> dt.date | None:
    match = _DATE.search(text or "")
    if not match:
        return None
    return parse_date(
        f"{match.group(1)}/{match.group(2)}/{match.group(3)}", dayfirst=True
    )


def _first_int(text: str) -> int | None:
    match = _INT.search(_strip_dates(text).replace(" ", ""))
    return int(match.group(1)) if match else None


def _amount(text: str) -> float | None:
    """A Tunisian dinar amount: space grouping, comma decimal, three decimals.

    The convention is stated rather than inferred. ``5 000 000,000`` is five
    million, and there are far too few numbers on one page for the generic
    document-wide inference to have anything to work from.
    """
    return parse_amount(text, decimal_sep=",")


# --------------------------------------------------------------------------- #
# Parsing one reading
# --------------------------------------------------------------------------- #


def extract_from_boxes(reading: PageReading, title_hint: str = "") -> DeclarationFacts | None:
    """Read a full set of facts from one recognised page, or return ``None``.

    Returning ``None`` is how a wrong rotation is rejected, and equally how a
    page that is not a declaration at all is skipped.
    """
    boxes = reading.boxes
    if not boxes:
        return None

    title = title_hint
    if not title:
        for box in boxes:
            if _TITLE_PATTERN.search(box.text):
                title = box.text
                break
    if not title:
        return None

    anchors = build_anchors(boxes)
    row_gap = (reading.height or 1650.0) * 0.10

    def read(key: str) -> tuple[str, float]:
        anchor = anchors.get(key)
        if anchor is None:
            return "", 1.0
        value = _value_below(anchor, boxes, max_gap=row_gap)
        return (value.text, value.confidence) if value else ("", 1.0)

    taux_text, taux_conf = read("taux")
    quantite_text, quantite_conf = read("quantite")
    sous_text, sous_conf = read("date_souscription")
    remb_text, remb_conf = read("date_remboursement")
    prix_text, _ = read("prix_unitaire")
    montant_text, _ = read("montant")
    libelle_text, _ = read("libelle")

    taux = _first_percent(taux_text)
    if taux is None:
        # The libellé repeats the rate on every document seen so far, which
        # makes it a free second source when the Taux column is unreadable.
        taux = _first_percent(libelle_text)

    quantite = _first_int(quantite_text)
    souscription = _first_date(sous_text)
    remboursement = _first_date(remb_text)

    missing = [
        name
        for name, value in (
            ("taux", taux),
            ("quantite", quantite),
            ("date de souscription", souscription),
            ("date de remboursement", remboursement),
        )
        if value is None
    ]
    if missing:
        log.debug(
            "page %d rot %d: incomplete (%s)",
            reading.page_number,
            reading.rotation,
            ", ".join(missing),
        )
        return None

    assert taux is not None and quantite is not None
    assert souscription is not None and remboursement is not None

    return DeclarationFacts(
        title=title,
        taux=taux,
        quantite=quantite,
        date_souscription=souscription,
        date_remboursement=remboursement,
        prix_unitaire=_amount(prix_text),
        montant=_amount(montant_text),
        source_page=reading.page_number,
        libelle=libelle_text,
        confidence=min(taux_conf, quantite_conf, sous_conf, remb_conf),
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _render(document: Any, index: int, dpi: int):
    from pdf2csv.core import grid

    page = document[index]
    bitmap = page.render(scale=dpi / 72.0, grayscale=True)
    try:
        return grid.to_grayscale_array(bitmap.to_pil())
    finally:
        for obj in (bitmap, page):
            close = getattr(obj, "close", None)
            if callable(close):
                close()


def _rotations(image: Any):
    import cv2

    # Quarter turns first: these documents are landscape on a portrait page, so
    # an upright page is the rare case and trying it first costs a wasted pass.
    yield 270, cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    yield 90, cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    yield 0, image
    yield 180, cv2.rotate(image, cv2.ROTATE_180)


def extract_declarations(
    pdf_path: str,
    *,
    dpi: int = DEFAULT_DPI,
    progress: Any = None,
) -> list[DeclarationFacts]:
    """Read every declaration in a PDF.

    Opens with pypdfium2 rather than pdfplumber. One document in the sample set
    reports zero pages through pdfplumber while rendering perfectly through
    pdfium, and a scan has no text layer to lose by skipping pdfplumber anyway.
    """
    reason = ocr.unavailable_reason()
    if reason:
        raise RuntimeError(reason)

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf_path)
    facts: list[DeclarationFacts] = []
    try:
        total = len(document)
        if total == 0:
            raise ValueError("This PDF contains no pages that can be rendered.")

        for index in range(total):
            if progress:
                progress(index, total, f"Reading page {index + 1} of {total}")

            found = _read_page(_render(document, index, dpi), index + 1)
            if found is not None:
                facts.append(found)
            else:
                log.info("page %d: no declaration recognised", index + 1)
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()

    log.info("read %d declaration(s)", len(facts))
    return facts


def _read_page(image: Any, page_number: int) -> DeclarationFacts | None:
    """Recognise a page at each plausible rotation and keep what parses."""
    tried: list[int] = []

    for rotation, rotated in _rotations(image):
        boxes = ocr.recognise(rotated)
        reading = PageReading(
            page_number=page_number,
            rotation=rotation,
            boxes=boxes,
            width=float(rotated.shape[1]),
            height=float(rotated.shape[0]),
        )
        if reading.wide_fraction < _WIDE_BOX_MIN:
            # Text still running down the page: this quarter turn is wrong and
            # parsing it would only waste time.
            continue

        tried.append(rotation)
        parsed = extract_from_boxes(reading)
        if parsed is not None:
            log.info(
                "page %d: read at rotation %d (confidence %.0f%%)",
                page_number,
                rotation,
                parsed.confidence * 100,
            )
            return parsed

    if tried:
        log.info(
            "page %d: text found at rotation(s) %s but no declaration parsed",
            page_number,
            ", ".join(map(str, tried)),
        )
    return None
