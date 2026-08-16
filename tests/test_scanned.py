"""Tests for the scanned path: geometry, then a full OCR round trip.

The geometry tests are pure and fast and always run. The round-trip test is
marked ``ocr`` and skips when the add-on is absent, because a deployment
without it is a supported configuration, not a broken one.

The round trip is the test that matters. It takes the same statement as the
digital fixtures, flattens it to a JPEG with no text layer, and asserts the
pipeline recovers the same numbers. Everything between — routing, deskew, line
detection, recognition, cell assignment, digit repair, reconciliation — has to
work for it to pass.
"""

from __future__ import annotations

import numpy as np
import pytest

from pdf2csv.core import grid
from pdf2csv.models import PageKind, TextBox

cv2 = pytest.importorskip("cv2", reason="OCR extra not installed")


def box(text, x0, y0, x1, y1, conf=1.0) -> TextBox:
    return TextBox(text=text, x0=x0, y0=y0, x1=x1, y1=y1, confidence=conf)


class TestRowGrouping:
    def test_groups_by_vertical_position(self):
        boxes = [
            box("A", 10, 100, 40, 112),
            box("B", 200, 101, 240, 113),
            box("C", 10, 140, 40, 152),
            box("D", 200, 141, 240, 153),
        ]
        rows = grid.group_rows(boxes)
        assert len(rows) == 2
        assert [b.text for b in rows[0]] == ["A", "B"]

    def test_reading_order_within_a_row(self):
        boxes = [box("B", 200, 100, 240, 112), box("A", 10, 100, 40, 112)]
        assert [b.text for b in grid.group_rows(boxes)[0]] == ["A", "B"]

    def test_uses_horizontal_rules_when_present(self):
        boxes = [box("A", 10, 105, 40, 117), box("B", 10, 145, 40, 157)]
        rows = grid.group_rows(boxes, y_rules=[100.0, 140.0, 180.0])
        assert len(rows) == 2


class TestColumnCorridors:
    def test_finds_the_whitespace_channel(self):
        rows = [
            [box("2025-03-01", 10, y, 90, y + 12), box("1,234.56", 300, y, 380, y + 12)]
            for y in range(100, 300, 20)
        ]
        boundaries = grid.infer_column_boundaries(rows, 400)
        assert len(boundaries) - 1 == 2

    def test_right_aligned_amounts_stay_one_column(self):
        """Clustering left edges would scatter these across phantom columns.

        Amounts are right-aligned, so their x-starts differ by the width of the
        number. The corridor between the label and the amount is what is
        stable, and that is what this algorithm keys on.
        """
        rows = []
        for index, (label, amount_x0) in enumerate(
            [("Rent", 320), ("Electricity supply", 300), ("Fee", 350)]
        ):
            y = 100 + index * 20
            rows.append([box(label, 10, y, 120, y + 12), box("x", amount_x0, y, 390, y + 12)])

        boundaries = grid.infer_column_boundaries(rows, 400)
        assert len(boundaries) - 1 == 2

    def test_vertical_rules_win_when_drawn(self):
        rows = [[box("A", 10, 100, 90, 112), box("B", 210, 100, 290, 112)]]
        boundaries = grid.infer_column_boundaries(rows, 400, x_rules=[0.0, 200.0, 400.0])
        assert boundaries == [0.0, 200.0, 400.0]


class TestTableBand:
    def test_separates_the_letterhead_from_the_body(self):
        rows = []
        # Letterhead: three lines at an irregular 14pt pitch.
        for y in (40, 54, 68):
            rows.append([box("BANK", 10, y, 200, y + 10)])
        # Body: eight rows on a constant 18pt pitch.
        for index in range(8):
            y = 110 + index * 18
            rows.append([box("a", 10, y, 90, y + 12), box("b", 300, y, 380, y + 12)])
        # Footer, far away.
        rows.append([box("Page 1", 10, 700, 60, 712)])

        band = grid.select_table_band(rows)
        assert band is not None
        start, end = band
        assert (end - start + 1) == 8
        assert start == 3

    def test_tolerates_one_wrapped_row(self):
        """A description wrapping to a second line doubles the pitch once.
        The table must not be cut in half at that point."""
        rows = []
        pitches = [18, 18, 36, 18, 18, 18, 18]
        y = 100
        rows.append([box("a", 10, y, 90, y + 12)])
        for pitch in pitches:
            y += pitch
            rows.append([box("a", 10, y, 90, y + 12)])

        band = grid.select_table_band(rows)
        assert band is not None
        assert band[1] - band[0] + 1 == len(rows)

    def test_returns_none_without_a_clear_rhythm(self):
        rows = [
            [box("a", 10, y, 90, y + 12)] for y in (10, 60, 75, 200, 205, 400)
        ]
        assert grid.select_table_band(rows) is None


class TestDeskew:
    def test_detects_and_corrects_a_tilt(self):
        image = np.full((600, 800), 255, np.uint8)
        for index in range(8):
            y = 80 + index * 60
            cv2.line(image, (60, y), (740, y), 0, 2)

        matrix = cv2.getRotationMatrix2D((400, 300), 2.0, 1.0)
        tilted = cv2.warpAffine(
            image, matrix, (800, 600), borderMode=cv2.BORDER_REPLICATE
        )

        # Assert the property that matters — the page comes out straight —
        # rather than the sign convention, which is OpenCV's and not ours.
        assert abs(grid.estimate_skew(tilted)) == pytest.approx(2.0, abs=0.4)

        corrected, applied = grid.deskew(tilted)
        assert abs(applied) == pytest.approx(2.0, abs=0.4)
        assert abs(grid.estimate_skew(corrected)) < 0.3

    def test_leaves_a_straight_page_alone(self):
        """Rotating costs interpolation blur; do not pay it for nothing."""
        image = np.full((600, 800), 255, np.uint8)
        for index in range(8):
            y = 80 + index * 60
            cv2.line(image, (60, y), (740, y), 0, 2)

        _corrected, applied = grid.deskew(image)
        assert applied == 0.0


class TestGridAssembly:
    def test_two_boxes_in_one_cell_are_joined_not_overwritten(self):
        rows = [[box("USD", 10, 100, 40, 112), box("1,234.56", 45, 100, 120, 112)]]
        text_grid, _ = grid.build_grid(rows, [0.0, 200.0])
        assert text_grid[0][0] == "USD 1,234.56"

    def test_confidence_is_the_worst_in_the_cell(self):
        rows = [[box("1,234", 10, 100, 60, 112, 0.99), box(".56", 62, 100, 90, 112, 0.40)]]
        _, confidences = grid.build_grid(rows, [0.0, 200.0])
        assert confidences[0][0] == pytest.approx(0.40)


# --------------------------------------------------------------------------- #
# Full round trip
# --------------------------------------------------------------------------- #


@pytest.mark.ocr
@pytest.mark.slow
class TestScannedRoundTrip:
    """The same statement, flattened to a JPEG, must produce the same numbers."""

    @pytest.fixture(scope="class")
    def scanned_result(self, request):
        from pdf2csv import run
        from pdf2csv.core import ocr

        if not ocr.is_available():
            pytest.skip("OCR extra not installed")

        path = request.path.parent / "fixtures" / "pdfs" / "statement_scanned.pdf"
        if not path.is_file():
            pytest.skip("scanned fixture missing — run tests/fixtures/make_fixtures.py")
        return run(path)

    def test_pages_are_routed_to_ocr(self, scanned_result):
        assert scanned_result.meta.page_kinds == [PageKind.SCANNED, PageKind.SCANNED]

    def test_recovers_exactly_the_transaction_rows(self, scanned_result):
        """The same ten rows the digital fixture yields — no letterhead, no
        footer, nothing invented. Text outside the ruled table must be dropped
        rather than becoming junk rows in the middle of a ledger."""
        assert len(scanned_result.dataframe) == 10

    def test_recovers_the_closing_balance(self, scanned_result):
        frame = scanned_result.dataframe
        numeric = [c for c in frame.columns if frame[c].dtype.kind == "f"]
        assert numeric, f"no numeric column recovered from the scan: {list(frame.columns)}"

        closing = frame[numeric[-1]].dropna()
        assert closing.iloc[-1] == pytest.approx(16610.90, abs=0.01)

    def test_the_figures_reconcile(self, scanned_result):
        """The strongest possible statement about the scanned path: the numbers
        read off a JPEG add up to the totals printed in the same JPEG."""
        report = scanned_result.report
        assert report.passed, report.summary()

        totals = next((c for c in report.checks if c.id == "stated_totals"), None)
        assert totals is not None and totals.passed

        balance = next((c for c in report.checks if c.id == "running_balance"), None)
        assert balance is not None and balance.passed

    def test_the_report_records_that_it_came_from_a_scan(self, scanned_result):
        assert scanned_result.meta.n_scanned == 2
        assert any(c.id == "ocr_confidence" for c in scanned_result.report.checks)


@pytest.mark.ocr
def test_ocr_models_are_bundled_not_downloaded():
    """The offline claim, checked rather than assumed.

    A cached model on a developer machine hides a first-run download perfectly.
    This asserts the weights ship inside the wheel, which is what makes the
    scanned path work on an air-gapped desktop.
    """
    from pdf2csv.core import ocr

    if not ocr.is_available():
        pytest.skip("OCR extra not installed")

    report = ocr.model_report()
    assert report["available"]
    assert len(report["models"]) >= 2
    assert sum(m["size_mb"] for m in report["models"]) > 5
