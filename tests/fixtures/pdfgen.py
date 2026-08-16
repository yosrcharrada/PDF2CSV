"""A minimal PDF writer, so fixtures are real PDFs with no extra dependency.

Adding ReportLab purely to build test files would put a dependency in the dev
environment that never ships, and committing binary PDFs with no source leaves
nobody able to adjust a fixture six months later. This module is about 150
lines and produces genuine PDF 1.4 files that pdfplumber parses exactly as it
parses a bank's output — same text-positioning operators, same ruling lines.

Deliberately not general: one font, no compression, no transparency. Enough to
lay out a statement, and small enough to read in one sitting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

A4 = (595.28, 841.89)

# Helvetica advance widths in 1/1000 em, for the characters a statement uses.
# Enough for right-aligning amounts, which is what actually matters here —
# without it the amount columns do not line up and the fixture stops resembling
# a real document.
_WIDTHS = {
    **dict.fromkeys("0123456789", 556),
    **dict.fromkeys("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 667),
    **dict.fromkeys("abcdefghijklmnopqrstuvwxyz", 556),
    " ": 278, ".": 278, ",": 278, ":": 278, ";": 278,
    "(": 333, ")": 333, "-": 333, "/": 278, "'": 191, "\"": 355,
    "%": 889, "&": 667, "*": 389, "+": 584, "=": 584,
}
_DEFAULT_WIDTH = 556


def text_width(text: str, size: float) -> float:
    """Approximate rendered width in points."""
    return sum(_WIDTHS.get(ch, _DEFAULT_WIDTH) for ch in text) * size / 1000.0


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


@dataclass
class Page:
    """One page. Origin is bottom-left, as in the PDF coordinate system."""

    width: float = A4[0]
    height: float = A4[1]
    ops: list[str] = field(default_factory=list)

    # Set to make this a scanned page: a full-bleed JPEG and no text layer,
    # which is exactly what a flatbed scanner produces.
    image_jpeg: bytes | None = None
    image_width: int = 0
    image_height: int = 0

    def set_image(self, jpeg: bytes, width: int, height: int) -> None:
        self.image_jpeg = jpeg
        self.image_width = width
        self.image_height = height

    # --- text ---------------------------------------------------------------
    def text(self, x: float, y: float, value: str, size: float = 9.0, bold: bool = False) -> None:
        font = "F2" if bold else "F1"
        self.ops.append(
            f"BT /{font} {size:g} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_escape(value)}) Tj ET"
        )

    def text_right(
        self, right: float, y: float, value: str, size: float = 9.0, bold: bool = False
    ) -> None:
        """Right-align at ``right`` — how every amount column in finance is set."""
        self.text(right - text_width(value, size), y, value, size, bold)

    def text_centre(
        self, centre: float, y: float, value: str, size: float = 9.0, bold: bool = False
    ) -> None:
        self.text(centre - text_width(value, size) / 2.0, y, value, size, bold)

    # --- vectors ------------------------------------------------------------
    def line(self, x0: float, y0: float, x1: float, y1: float, width: float = 0.6) -> None:
        self.ops.append(f"{width:g} w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S")

    def rect(self, x: float, y: float, w: float, h: float, width: float = 0.6) -> None:
        self.ops.append(f"{width:g} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")

    def content(self) -> bytes:
        # cp1252 covers the accented characters a French or Arabic-region
        # statement uses, and matches the WinAnsiEncoding declared on the fonts.
        return "\n".join(self.ops).encode("cp1252", errors="replace")


class PdfBuilder:
    """Accumulates pages and writes a valid PDF 1.4 file."""

    def __init__(self) -> None:
        self.pages: list[Page] = []

    def add_page(self, width: float = A4[0], height: float = A4[1]) -> Page:
        page = Page(width=width, height=height)
        self.pages.append(page)
        return page

    def save(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.to_bytes())
        return destination

    def to_bytes(self) -> bytes:
        if not self.pages:
            raise ValueError("a PDF needs at least one page")

        # 1 catalog, 2 pages, 3 and 4 fonts, then two or three objects per page
        # (the page, its content stream, and an image XObject if it has one).
        objects: dict[int, bytes] = {}
        next_id = 5
        layout: list[tuple[int, int, int | None, Page]] = []

        for page in self.pages:
            page_id, next_id = next_id, next_id + 1
            content_id, next_id = next_id, next_id + 1
            image_id: int | None = None
            if page.image_jpeg:
                image_id, next_id = next_id, next_id + 1
            layout.append((page_id, content_id, image_id, page))

        kids = " ".join(f"{page_id} 0 R" for page_id, _, _, _ in layout)

        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[2] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode("ascii")
        )
        objects[3] = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        )
        objects[4] = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )

        for page_id, content_id, image_id, page in layout:
            if image_id is not None:
                # Draw the scan full-bleed. The cm operator maps the image's
                # unit square onto the whole page.
                body = (
                    f"q {page.width:.2f} 0 0 {page.height:.2f} 0 0 cm /Im0 Do Q"
                ).encode("ascii")
                resources = f"/XObject << /Im0 {image_id} 0 R >>"
                assert page.image_jpeg is not None
                objects[image_id] = (
                    f"<< /Type /XObject /Subtype /Image "
                    f"/Width {page.image_width} /Height {page.image_height} "
                    f"/ColorSpace /DeviceGray /BitsPerComponent 8 "
                    f"/Filter /DCTDecode /Length {len(page.image_jpeg)} >>\nstream\n"
                ).encode("ascii") + page.image_jpeg + b"\nendstream"
            else:
                body = page.content()
                resources = "/Font << /F1 3 0 R /F2 4 0 R >>"

            objects[page_id] = (
                f"<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {page.width:.2f} {page.height:.2f}] "
                f"/Resources << {resources} >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
            objects[content_id] = (
                f"<< /Length {len(body)} >>\nstream\n".encode("ascii")
                + body
                + b"\nendstream"
            )

        return self._assemble(objects)

    @staticmethod
    def _assemble(objects: dict[int, bytes]) -> bytes:
        out = bytearray(b"%PDF-1.4\n")
        # A binary comment marks the file as binary for tools that sniff it.
        out += b"%\xe2\xe3\xcf\xd3\n"

        highest = max(objects)
        offsets: dict[int, int] = {}

        for number in range(1, highest + 1):
            body = objects.get(number)
            if body is None:
                continue
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

        xref_offset = len(out)
        size = highest + 1
        out += f"xref\n0 {size}\n".encode("ascii")
        # Every entry is exactly 20 bytes — the format is fixed-width and
        # readers will reject the file if it is not.
        out += b"0000000000 65535 f \n"
        for number in range(1, size):
            offset = offsets.get(number, 0)
            kind = b"n" if number in offsets else b"f"
            out += f"{offset:010d} 00000 ".encode("ascii") + kind + b" \n"

        out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n".encode(
            "ascii"
        )
        out += b"%%EOF\n"
        return bytes(out)


# --------------------------------------------------------------------------- #
# Layout helper
# --------------------------------------------------------------------------- #


@dataclass
class TableLayout:
    """Column geometry shared by the fixture builders."""

    left: float
    right: float
    column_x: list[float]
    """Left edge of each column."""

    align: list[str]
    """``"l"`` or ``"r"`` per column. Amounts are right-aligned, as in reality."""

    row_height: float = 18.0
    font_size: float = 9.0

    def draw_row(
        self,
        page: Page,
        y: float,
        values: list[str],
        *,
        bold: bool = False,
        rule: bool = False,
    ) -> None:
        for index, value in enumerate(values):
            if not value:
                continue
            if self.align[index] == "r":
                edge = (
                    self.column_x[index + 1] - 4 if index + 1 < len(self.column_x) else self.right
                )
                page.text_right(edge, y, value, self.font_size, bold)
            else:
                page.text(self.column_x[index] + 2, y, value, self.font_size, bold)
        if rule:
            page.line(self.left, y - 5, self.right, y - 5)

    def draw_verticals(self, page: Page, top: float, bottom: float) -> None:
        for x in [*self.column_x, self.right]:
            page.line(x, bottom, x, top)
