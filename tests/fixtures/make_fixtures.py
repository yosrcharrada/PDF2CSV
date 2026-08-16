"""Build the fixture PDFs. Run this to regenerate ``tests/fixtures/pdfs``.

    python tests/fixtures/make_fixtures.py

The generated PDFs are committed. Committing them keeps the test suite fast and
hermetic; committing this script alongside keeps them editable, which matters
the first time someone needs to add a column to reproduce a client's layout.

Each fixture exercises a specific failure mode, named in its docstring. Between
them they cover the four combinations of ruled/borderless and Anglo/European
that the extractor has to tell apart without being told.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pdfgen import PdfBuilder, TableLayout

OUT = Path(__file__).parent / "pdfs"

TOP = 760.0
ROW_HEIGHT = 18.0
BOTTOM_MARGIN = 90.0


# --------------------------------------------------------------------------- #
# 1. Ruled statement, Anglo separators, header repeated on page 2
# --------------------------------------------------------------------------- #

RULED_HEADER = ["Date", "Description", "Reference", "Debit", "Credit", "Balance"]

RULED_ROWS = [
    ["01/03/2025", "Opening balance", "", "", "", "12,450.00"],
    ["03/03/2025", "Card payment - STATIONERY", "0041123", "340.25", "", "12,109.75"],
    ["07/03/2025", "Transfer received", "0041187", "", "1,200.50", "13,310.25"],
    ["14/03/2025", "Direct debit - UTILITIES", "0041234", "512.00", "", "12,798.25"],
    ["18/03/2025", "Cheque deposit", "0041318", "", "2,000.00", "14,798.25"],
    ["21/03/2025", "Salary - MARCH", "0041402", "", "3,150.00", "17,948.25"],
    ["25/03/2025", "Card payment - TRAVEL", "0041477", "1,024.80", "", "16,923.45"],
    ["27/03/2025", "Bank charges", "0041520", "35.00", "", "16,888.45"],
    ["28/03/2025", "Interest paid", "0041556", "", "12.35", "16,900.80"],
    ["31/03/2025", "Card payment - SUPPLIES", "0041610", "289.90", "", "16,610.90"],
]

RULED_TOTAL = ["", "TOTAL", "", "2,201.95", "6,362.85", "16,610.90"]


def _ruled_layout() -> TableLayout:
    return TableLayout(
        left=40.0,
        right=555.0,
        column_x=[40.0, 110.0, 300.0, 380.0, 450.0, 505.0],
        align=["l", "l", "l", "r", "r", "r"],
    )


def _draw_ruled_page(page, layout: TableLayout, rows: list[list[str]], *, title: str | None) -> None:
    y = TOP
    if title:
        page.text(40, y + 46, "NORTHGATE COMMERCIAL BANK", 13, bold=True)
        page.text(40, y + 32, "Statement of Account", 10)
        page.text(40, y + 20, "Account 40-12-88  61847205    Period 01 Mar 2025 - 31 Mar 2025", 8)

    top_rule = y + 13
    layout.draw_row(page, y, RULED_HEADER, bold=True)
    page.line(layout.left, y - 5, layout.right, y - 5)
    y -= ROW_HEIGHT

    for row in rows:
        layout.draw_row(page, y, row)
        page.line(layout.left, y - 5, layout.right, y - 5)
        y -= ROW_HEIGHT

    page.line(layout.left, top_rule, layout.right, top_rule)
    layout.draw_verticals(page, top_rule, y + ROW_HEIGHT - 5)


def build_ruled_statement(path: Path, rows: list[list[str]], total_row: list[str] | None) -> None:
    """Ruled grid + repeated header on page 2. Exercises the lattice strategy,
    header de-duplication across pages, and identifier columns with leading
    zeros that must not become floats."""
    builder = PdfBuilder()
    layout = _ruled_layout()

    first, second = rows[:6], rows[6:]

    page = builder.add_page()
    _draw_ruled_page(page, layout, first, title="NORTHGATE")
    page.text(40, 60, "Page 1 of 2", 8)

    page = builder.add_page()
    tail = list(second) + ([total_row] if total_row else [])
    _draw_ruled_page(page, layout, tail, title=None)
    page.text(40, 60, "Page 2 of 2", 8)
    page.text(40, 44, "Continued from page 1. Please retain for your records.", 8)

    builder.save(path)


# --------------------------------------------------------------------------- #
# 2. Borderless statement, European separators, accented headers
# --------------------------------------------------------------------------- #

FR_HEADER = ["Date", "Libellé", "Débit", "Crédit", "Solde"]

FR_ROWS = [
    ["01/04/2025", "Solde initial", "", "", "8.750,00"],
    ["04/04/2025", "Virement reçu - CLIENT SARL", "", "2.310,45", "11.060,45"],
    ["09/04/2025", "Prélèvement EDF", "189,90", "", "10.870,55"],
    ["15/04/2025", "Achat carte - FOURNITURES", "1.245,00", "", "9.625,55"],
    ["22/04/2025", "Remise de chèque", "", "4.500,00", "14.125,55"],
    ["28/04/2025", "Frais de tenue de compte", "12,50", "", "14.113,05"],
    ["30/04/2025", "Intérêts créditeurs", "", "33,20", "14.146,25"],
]

FR_TOTAL = ["", "Total des mouvements", "1.447,40", "6.843,65", "14.146,25"]


def build_borderless_statement(path: Path) -> None:
    """No ruling lines at all, European decimal commas, accented headers.

    Exercises the text/whitespace strategy, document-wide decimal separator
    inference (``1.245,00`` must be 1245.00, not 1.245), and UTF-8 headers
    surviving into the CSV.
    """
    builder = PdfBuilder()
    layout = TableLayout(
        left=40.0,
        right=555.0,
        column_x=[40.0, 115.0, 330.0, 415.0, 490.0],
        align=["l", "l", "r", "r", "r"],
    )

    page = builder.add_page()
    page.text(40, 800, "BANQUE DU SUD", 13, bold=True)
    page.text(40, 786, "Relevé de compte", 10)
    page.text(40, 774, "Compte 08 123 4567890 12   Période du 01/04/2025 au 30/04/2025", 8)

    y = TOP
    layout.draw_row(page, y, FR_HEADER, bold=True)
    y -= ROW_HEIGHT * 1.4

    for row in FR_ROWS[:4]:
        layout.draw_row(page, y, row)
        y -= ROW_HEIGHT
    page.text(40, 60, "Page 1 / 2", 8)

    page = builder.add_page()
    y = TOP
    layout.draw_row(page, y, FR_HEADER, bold=True)
    y -= ROW_HEIGHT * 1.4
    for row in [*FR_ROWS[4:], FR_TOTAL]:
        layout.draw_row(page, y, row)
        y -= ROW_HEIGHT
    page.text(40, 60, "Page 2 / 2", 8)

    builder.save(path)


# --------------------------------------------------------------------------- #
# 3. Deliberately broken statement
# --------------------------------------------------------------------------- #


def build_broken_statement(path: Path) -> None:
    """A statement with one transaction missing from the body.

    The totals row still states the full figures, so both the stated-totals
    check and the running-balance check must fail — and the running-balance
    check must name the specific row where the sequence breaks. If this fixture
    ever passes validation, the gate is broken and every other test is
    meaningless.
    """
    rows = [row for row in RULED_ROWS if row[2] != "0041402"]  # drop the salary credit
    build_ruled_statement(path, rows, RULED_TOTAL)


# --------------------------------------------------------------------------- #
# 4. A document with no table at all
# --------------------------------------------------------------------------- #


def build_letter(path: Path) -> None:
    """Prose only. Must produce a clean 'no table found' result, not a crash."""
    builder = PdfBuilder()
    page = builder.add_page()
    page.text(40, 780, "NORTHGATE COMMERCIAL BANK", 13, bold=True)
    y = 740
    for line in [
        "Dear customer,",
        "",
        "We are writing to confirm that the terms applying to your business",
        "current account will change with effect from 1 June 2025. The revised",
        "schedule of charges is available on request from your relationship",
        "manager or at any branch.",
        "",
        "No action is required from you.",
        "",
        "Yours faithfully,",
        "Customer Operations",
    ]:
        page.text(40, y, line, 10)
        y -= 16
    builder.save(path)


# --------------------------------------------------------------------------- #
# 5. A genuine scan — no text layer at all
# --------------------------------------------------------------------------- #


def build_scanned_statement(source: Path, path: Path, *, dpi: int = 200) -> None:
    """Rasterise a digital fixture and rebuild it as a scan.

    Produces a PDF whose pages carry a JPEG and no text layer whatsoever —
    which is what the router must detect and what OCR must then read. Faking
    this with a low-quality text layer would test nothing: the interesting
    property is precisely that ``extract_text`` returns nothing at all.

    Needs pypdfium2 and OpenCV, so it is skipped when the OCR extra is absent.
    """
    import cv2
    import numpy as np
    import pypdfium2 as pdfium

    builder = PdfBuilder()
    document = pdfium.PdfDocument(str(source))
    try:
        for index in range(len(document)):
            bitmap = document[index].render(scale=dpi / 72.0, grayscale=True)
            image = np.asarray(bitmap.to_pil())

            # A touch of sensor noise, so the fixture is not an unrealistically
            # perfect bitmap. Deterministic seed keeps the test reproducible.
            rng = np.random.default_rng(1234 + index)
            noisy = np.clip(
                image.astype(np.int16) + rng.normal(0, 3, image.shape).astype(np.int16),
                0,
                255,
            ).astype(np.uint8)

            ok, encoded = cv2.imencode(".jpg", noisy, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                raise RuntimeError("JPEG encoding failed")

            page = builder.add_page()
            page.set_image(encoded.tobytes(), noisy.shape[1], noisy.shape[0])
    finally:
        document.close()

    builder.save(path)


# --------------------------------------------------------------------------- #

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ruled = OUT / "statement_ruled_2page.pdf"
    build_ruled_statement(ruled, RULED_ROWS, RULED_TOTAL)
    build_borderless_statement(OUT / "statement_borderless_fr.pdf")
    build_broken_statement(OUT / "statement_broken.pdf")
    build_letter(OUT / "letter_no_table.pdf")

    try:
        build_scanned_statement(ruled, OUT / "statement_scanned.pdf")
    except ImportError as exc:
        print(f"  (skipped the scanned fixture: {exc})")

    for pdf in sorted(OUT.glob("*.pdf")):
        print(f"  {pdf.name:34s} {pdf.stat().st_size:>7,} bytes")


if __name__ == "__main__":
    main()
