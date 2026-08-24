"""The fiche reader: a wide table split across pages, several rows to a page.

No client document appears here. The grid work is exercised against a drawn
table whose ruling lines are placed by the test, and the text handling against
constructed boxes, so every expectation is one the test itself establishes.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

import numpy as np
import pytest

from pdf2csv.declarations.fiche import (
    BOTH_HALVES,
    COLUMN_SPECS,
    INSTRUMENT_HALF,
    SUBSCRIBER_HALF,
    PageTable,
    _column_edges,
    _facts_from_row,
    _half_of,
    _join,
    _row_bands,
    _spread,
    _table_band,
    read_page_table,
)
from pdf2csv.models import TextBox

SPECS = {spec.key: spec for spec in COLUMN_SPECS}


def box(text: str, x0: float, y0: float, x1: float, y1: float) -> TextBox:
    return TextBox(text=text, x0=x0, y0=y0, x1=x1, y1=y1)


def ruled_page(
    edges: list[int],
    top: int,
    bottom: int,
    width: int = 1200,
    height: int = 900,
) -> np.ndarray:
    """A blank page with vertical ruling lines, as a scan of one would arrive."""
    page = np.full((height, width), 255, dtype=np.uint8)
    for x in edges:
        page[top:bottom, x] = 0
    # Ink in every column, so the band is found by width the way a real one is.
    page[top + 4 : bottom - 4 : 6, edges[0] : edges[-1]] = 40
    return page


class TestColumnsComeFromTheRuling:
    """Boundaries are taken from the printed rules, not from the headings.

    A heading can wrap, merge with its neighbour or sit anywhere in its cell. A
    ruling line is a single unambiguous x, which is why it is preferred.
    """

    def test_every_rule_becomes_a_boundary(self):
        page = ruled_page([100, 300, 500, 900], top=200, bottom=400)
        band = _table_band(page < 160)
        assert band is not None
        edges = _column_edges(page < 160, band)
        for expected in (100, 300, 500, 900):
            assert any(abs(edge - expected) <= 2 for edge in edges), edges

    def test_the_table_is_found_by_width_not_by_position(self):
        """A letterhead is tall and narrow; the table is the wide band."""
        page = ruled_page([100, 300, 500, 900], top=400, bottom=600)
        page[80:200, 120:260] = 30  # a letterhead above it
        top, _bottom, left, right = _table_band(page < 160)
        assert 380 <= top <= 420
        assert right - left > 700


class TestRowsAbsorbWrappedCells:
    """A cell that wraps puts its first line above the row's baseline.

    Grouping by position alone therefore finds more lines than there are rows,
    and the extra ones have to be folded into the row they belong to rather
    than becoming rows of their own or being dropped.
    """

    edges: ClassVar[list[float]] = [0.0, 100.0, 200.0, 300.0, 400.0]

    def test_a_narrow_line_is_not_a_row_of_its_own(self):
        boxes = [
            box("500", 210, 40, 260, 60),  # first line of a wrapped cell
            box("A", 10, 100, 60, 120),
            box("8,40%", 110, 100, 160, 120),
            box("000", 210, 100, 260, 120),
            box("31/07", 310, 100, 360, 120),
        ]
        bands = _row_bands(boxes, self.edges)
        assert len(bands) == 1
        top, bottom = bands[0]
        assert top <= 40 and bottom >= 120

    def test_a_fragment_joins_the_row_below_it(self):
        """The last line of a wrapped cell sits on the row's baseline, so the
        line above belongs to that row and not to the one before it."""
        boxes = [
            box("A", 10, 20, 60, 40),
            box("8,40%", 110, 20, 160, 40),
            box("000", 210, 20, 260, 40),
            box("31/07", 310, 20, 360, 40),
            box("15", 210, 80, 260, 100),  # wrapped first line of row two
            box("B", 10, 140, 60, 160),
            box("7,84%", 110, 140, 160, 160),
            box("696", 210, 140, 260, 160),
            box("03/08", 310, 140, 360, 160),
        ]
        bands = _row_bands(boxes, self.edges)
        assert len(bands) == 2
        assert bands[0][1] < 80, "the fragment must not fall in the first row"
        assert bands[1][0] <= 80, "the fragment must fall in the second row"


class TestMergedHeadingsAreSplitByWord:
    def test_each_word_goes_to_the_column_it_is_printed_over(self):
        """The recogniser returns the two date headings as one box. Cutting the
        string proportionally lands a character early and yields
        'souscriptio', which matches no heading at all."""
        edges = [0.0, 100.0, 200.0, 300.0]
        merged = box("souscription remboursement", 100, 10, 300, 30)
        assert _spread(merged, edges) == [(1, "souscription"), (2, "remboursement")]


class TestHeadingsAreMatchedLoosely:
    """Recognition of a heading is not reliable enough to demand an exact match.

    A lost column is not a visible failure: it is a row carrying a plausible
    wrong value, which is the worst kind.
    """

    @pytest.mark.parametrize(
        ("read_as", "key"),
        [
            ("datedesouscriptio", "date_souscription"),  # clipped by a merge
            ("lnteretbrut", "interet_brut"),  # l read for i
            ("quantile", "quantite"),  # t read for l
            ("nationalit", "nationality"),  # dropped ending
        ],
    )
    def test_a_misread_heading_is_still_recognised(self, read_as, key):
        assert SPECS[key].similarity(read_as) >= 0.82

    def test_the_two_date_headings_never_answer_for_each_other(self):
        """The one confusion that must never happen: it would put a maturity
        where a subscription belongs and the row would look entirely normal."""
        assert SPECS["date_souscription"].similarity("datederemboursement") < 0.5
        assert SPECS["date_remboursement"].similarity("datedesouscription") < 0.5


class TestReadingAPage:
    """The whole page reduced to named columns and rows, without recognition.

    The boxes stand in for what the recogniser returns; none of them spans two
    columns, so no cell is re-read and the test needs no OCR.
    """

    def build(self):
        edges = [40, 300, 480, 700, 900, 1100]
        page = ruled_page(edges, top=180, bottom=460, width=1200)
        boxes = [
            box("Libelle du Certificat", 50, 190, 280, 210),
            box("Taux", 320, 190, 400, 210),
            box("Quantite", 500, 190, 620, 210),
            box("Date de souscription", 720, 190, 880, 210),
            box("Date de remboursement", 920, 190, 1080, 210),
            box("SER BTKL 8.40% CD", 50, 280, 280, 300),
            box("8.40%", 320, 280, 400, 300),
            box("5", 500, 280, 560, 300),
            box("31/07/2026", 720, 280, 880, 300),
            box("31/07/2027", 920, 280, 1080, 300),
            box("UFSS BTKL 7.84% CD", 50, 380, 280, 400),
            box("7.84%", 320, 380, 400, 400),
            box("10", 500, 380, 560, 400),
            box("03/08/2026", 720, 380, 880, 400),
            box("12/10/2026", 920, 380, 1080, 400),
        ]
        return page, boxes

    def test_the_instrument_half_is_recognised(self):
        page, boxes = self.build()
        table = read_page_table(page, boxes)
        assert table is not None
        assert table.half == INSTRUMENT_HALF

    def test_both_rows_come_out_with_their_own_values(self):
        page, boxes = self.build()
        table = read_page_table(page, boxes)
        assert table.height == 2
        assert table.rows[0]["taux"] == "8.40%"
        assert table.rows[0]["date_souscription"] == "31/07/2026"
        assert table.rows[1]["quantite"] == "10"
        assert table.rows[1]["date_remboursement"] == "12/10/2026"

    def test_the_headings_are_not_mistaken_for_a_row(self):
        page, boxes = self.build()
        table = read_page_table(page, boxes)
        assert all("Taux" not in row.get("taux", "") for row in table.rows)


class TestJoiningTheHalves:
    """The halves are matched by position, the only thing that relates them."""

    def instrument(self, count: int) -> PageTable:
        return PageTable(
            half=INSTRUMENT_HALF,
            columns={},
            rows=[
                {
                    "libelle": f"S{i} BTKL 8.40% CD 31072026",
                    "taux": "8.40%",
                    "quantite": "2",
                    "prix_unitaire": "500 000.000",
                    "montant": "1000000.000",
                    "date_souscription": "31/07/2026",
                    "date_remboursement": "31/07/2027",
                }
                for i in range(count)
            ],
            document_date=dt.date(2026, 8, 3),
        )

    def subscribers(self, names: list[str]) -> PageTable:
        return PageTable(
            half=SUBSCRIBER_HALF,
            columns={},
            rows=[
                {
                    "subscriber_name": name,
                    "client_type": "PERSONNE PHYSIQUE",
                    "nationality": "TUNISIENNE",
                    "nature_of_identification": "CARTE D'IDENTITE",
                    "national_id": f"0000{index}",
                }
                for index, name in enumerate(names)
            ],
        )

    def test_each_row_keeps_its_own_subscriber(self):
        facts = _join([self.subscribers(["AAA BBB", "CCC DDD"]), self.instrument(2)])
        assert [f.subscriber.name for f in facts] == ["AAA BBB", "CCC DDD"]

    def test_mismatched_halves_leave_the_identity_empty(self):
        """Pairing a subscriber with the wrong instrument produces a row that
        looks entirely ordinary and is wrong about who bought what. Refusing to
        pair is the only safe answer."""
        facts = _join([self.subscribers(["AAA BBB"]), self.instrument(3)])
        assert len(facts) == 3
        assert all(f.subscriber is None for f in facts)

    def test_the_document_date_reaches_every_row(self):
        facts = _join([self.subscribers(["AAA BBB", "CCC DDD"]), self.instrument(2)])
        assert all(f.document_date == dt.date(2026, 8, 3) for f in facts)


class TestOneRowIntoFacts:
    row: ClassVar[dict[str, str]] = {
        "libelle": "SERBTKL8.40%CD31072026",
        "taux": "8.40%",
        "prix_unitaire": "500 000.000",
        "montant": "3500000.000",
        "quantite": "5",
        "date_souscription": "31/07/2026",
        "date_remboursement": "31/07/2027",
    }

    def facts(self, **over):
        row = {**self.row, **over}
        return _facts_from_row(row, {}, dt.date(2026, 8, 3), "BTK LEASING", 1.0, 2, 0)

    def test_the_figures_are_read_as_printed(self):
        facts = self.facts()
        assert facts.taux == pytest.approx(8.4)
        assert facts.montant == pytest.approx(3500000.0)
        assert facts.prix_unitaire == pytest.approx(500000.0)
        assert facts.date_souscription == dt.date(2026, 7, 31)
        assert facts.date_remboursement == dt.date(2027, 7, 31)

    def test_a_row_missing_a_date_is_skipped_not_guessed(self):
        assert self.facts(date_remboursement="") is None

    def test_the_quantity_falls_back_to_the_montant(self):
        """A quantité the recogniser could not read is recoverable, because the
        montant and the unit price give the same number."""
        assert self.facts(quantite="").quantite == 7


class TestAWholePageTable:
    """A fiche whose columns all fit on one page needs no joining.

    Splitting the table over two pages is a property of this printing, not of
    the document class: a wider page puts every column on one, and then each
    row already names its own subscriber.
    """

    def whole(self) -> PageTable:
        return PageTable(
            half=BOTH_HALVES,
            columns={},
            rows=[
                {
                    "subscriber_name": "AAA BBB",
                    "client_type": "PERSONNE PHYSIQUE",
                    "nationality": "TUNISIENNE",
                    "nature_of_identification": "CARTE D'IDENTITE",
                    "national_id": "00468256",
                    "libelle": "SER BTKL 8.40% CD 31072026",
                    "taux": "8.40%",
                    "quantite": "2",
                    "prix_unitaire": "500 000.000",
                    "montant": "1000000.000",
                    "date_souscription": "31/07/2026",
                    "date_remboursement": "31/07/2027",
                }
            ],
            document_date=dt.date(2026, 8, 3),
        )

    def test_the_row_keeps_the_subscriber_printed_beside_it(self):
        facts = _join([self.whole()])
        assert len(facts) == 1
        assert facts[0].subscriber.name == "AAA BBB"
        assert facts[0].subscriber.national_id == "00468256"

    def test_the_instrument_is_read_from_the_same_row(self):
        facts = _join([self.whole()])
        assert facts[0].date_remboursement == dt.date(2027, 7, 31)
        assert facts[0].taux == pytest.approx(8.4)

    def test_a_page_holding_both_halves_is_recognised_as_such(self):
        named = {
            0: "subscriber_name",
            1: "client_type",
            2: "libelle",
            3: "taux",
            4: "date_souscription",
            5: "date_remboursement",
        }
        assert _half_of(named) == BOTH_HALVES
        assert _half_of({0: "libelle", 1: "taux"}) is None
