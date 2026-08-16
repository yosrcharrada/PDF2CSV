"""Deciding, per page, whether to read text or run OCR.

This is the single biggest performance lever in the project. OCR costs 10-60
seconds per page; reading a text layer costs milliseconds. A 50-page document
with 4 scanned pages should pay the OCR bill 4 times, not 50 — getting that
wrong is the difference between a 6-second job and a 25-minute one.

Classification is per page and never per document. Finance PDFs routinely
staple a typed statement to a scanned annex, or insert a photographed
authorisation page in the middle of a digital ledger.
"""

from __future__ import annotations

from typing import Any

from pdf2csv.logging_setup import get_logger
from pdf2csv.models import PageKind

log = get_logger(__name__)


def classify_page(page: Any, *, min_chars: int = 50) -> PageKind:
    """Classify one open ``pdfplumber`` page.

    The character-count test carries most of the weight, but on its own it
    misclassifies two real cases, so both are handled explicitly:

    * A **sparse digital page** — a short summary table with 30 characters and
      no images. Character count alone calls it scanned and wastes an OCR pass
      producing a worse result than the text layer already had.
    * A **blank separator page** — no text, no images, no vectors. Neither
      path has anything to find, so skip it rather than spending a minute of
      OCR on white paper.
    """
    try:
        text = (page.extract_text() or "").strip()
    except Exception:  # a malformed content stream should not sink the document
        log.warning("page %s: text extraction raised, treating as scanned", page.page_number)
        return PageKind.SCANNED

    char_count = len(text)
    if char_count >= min_chars:
        return PageKind.DIGITAL

    has_images = bool(getattr(page, "images", None))
    has_vectors = bool(
        getattr(page, "lines", None)
        or getattr(page, "rects", None)
        or getattr(page, "curves", None)
    )

    if has_images:
        # Little text but a picture on the page: the picture holds the content.
        # This also catches the "thin text layer over a scan" case, where a bad
        # prior OCR pass left a page number behind and nothing else.
        return PageKind.SCANNED

    if char_count > 0:
        # Real characters and no image to explain them away — a genuinely
        # sparse digital page. The text layer is exact; OCR could only lose.
        return PageKind.DIGITAL

    if has_vectors:
        # Ruled lines but no text at all: a drawn table whose labels are part
        # of the image, or a form. Worth an OCR pass.
        return PageKind.SCANNED

    return PageKind.EMPTY


def classify_document(pdf: Any, *, min_chars: int = 50) -> list[PageKind]:
    """Classify every page of an already-open ``pdfplumber`` PDF.

    Takes an open document rather than a path because the pipeline has one open
    already, and reopening a large PDF purely to classify it doubles the parse
    cost for no benefit.
    """
    kinds = [classify_page(page, min_chars=min_chars) for page in pdf.pages]
    counts = {kind: kinds.count(kind) for kind in PageKind if kind in kinds}
    log.info(
        "routed %d pages: %s",
        len(kinds),
        ", ".join(f"{k.value}={v}" for k, v in counts.items()) or "none",
    )
    return kinds


def needs_ocr_fallback(page: Any, found_table: bool) -> bool:
    """Should a page classified digital be re-read through the scanned path?

    Only when the page carries an **image** as well as its text layer. That
    combination is the signature of a scan that an earlier tool OCR'd badly:
    the words are present but their positions are not trustworthy enough to
    find columns, and re-recognising the picture is worth the seconds.

    The image requirement is what stops this from firing on every cover letter
    and terms-and-conditions page in existence. Those have a perfect text layer
    and genuinely contain no table; OCR-ing them costs a minute each and
    produces a garbage one-column "table" out of the prose.
    """
    if found_table:
        return False
    return bool(getattr(page, "images", None))
