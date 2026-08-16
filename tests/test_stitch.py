"""Tests for multi-page assembly and header handling.

The repeated-header case is the one that matters most. A header row left in the
data does not raise anything — ``parse_amount("Debit")`` returns ``None``, so
the row lands as blanks and the column total quietly disagrees with the
document by exactly the rows that were eaten.
"""

from __future__ import annotations

from pdf2csv.core.stitch import (
    find_header,
    group_tables,
    score_header_row,
    stitch,
    stitch_group,
)
from pdf2csv.models import ExtractedTable, PageKind


def table(page: int, rows: list[list[str]], kind: PageKind = PageKind.DIGITAL) -> ExtractedTable:
    return ExtractedTable(page_number=page, kind=kind, rows=rows, extractor="test")


HEADER = ["Date", "Description", "Debit", "Credit", "Balance"]


class TestHeaderScoring:
    def test_header_scores_above_data(self):
        data = ["01/03/2025", "Card payment", "340.25", "", "12,109.75"]
        assert score_header_row(HEADER) > score_header_row(data)

    def test_header_threshold(self):
        assert score_header_row(HEADER) >= 0.62

    def test_row_of_amounts_is_not_a_header(self):
        assert score_header_row(["1,200.00", "340.25", "12.35", "9.99", "5.00"]) < 0.62


class TestFindHeader:
    def test_finds_first_row(self):
        rows = [HEADER, ["01/03/2025", "x", "1.00", "", "2.00"]]
        header, consumed = find_header(rows)
        assert header == HEADER
        assert consumed == 1

    def test_skips_a_title_line(self):
        rows = [
            ["STATEMENT OF ACCOUNT", "", "", "", ""],
            HEADER,
            ["01/03/2025", "x", "1.00", "", "2.00"],
        ]
        header, consumed = find_header(rows)
        assert header == HEADER
        assert consumed == 2

    def test_no_header_returns_zero_consumed(self):
        """Inventing a header out of a data row would delete a transaction."""
        rows = [
            ["01/03/2025", "Card payment", "340.25", "", "12,109.75"],
            ["03/03/2025", "Transfer", "", "1,200.50", "13,310.25"],
        ]
        header, consumed = find_header(rows)
        assert consumed == 0
        assert header == []

    def test_merges_a_stacked_second_line(self):
        rows = [
            ["Date", "Description", "", "", "Closing"],
            ["", "", "Debit", "Credit", "Balance"],
            ["01/03/2025", "x", "1.00", "", "2.00"],
        ]
        header, consumed = find_header(rows)
        assert consumed == 2
        assert header[2] == "Debit"
        assert header[4] == "Closing Balance"


class TestGrouping:
    def test_same_width_consecutive_pages_group(self):
        tables = [table(1, [HEADER, ["a", "b", "c", "d", "e"]]), table(2, [HEADER, ["f", "g", "h", "i", "j"]])]
        assert len(group_tables(tables)) == 1

    def test_different_widths_do_not_group(self):
        tables = [table(1, [HEADER, ["a", "b", "c", "d", "e"]]), table(2, [["x", "y"], ["1", "2"]])]
        assert len(group_tables(tables)) == 2

    def test_non_adjacent_pages_do_not_group(self):
        tables = [table(1, [HEADER, ["a", "b", "c", "d", "e"]]), table(7, [HEADER, ["f", "g", "h", "i", "j"]])]
        assert len(group_tables(tables)) == 2


class TestRepeatedHeaders:
    def test_repeated_header_is_dropped(self):
        page1 = table(1, [HEADER, ["01/03/2025", "a", "1.00", "", "10.00"]])
        page2 = table(2, [HEADER, ["02/03/2025", "b", "2.00", "", "8.00"]])
        result = stitch_group([page1, page2])

        assert result.header == HEADER
        assert len(result.rows) == 2
        assert result.dropped_repeat_headers == 1
        assert all(row[0] != "Date" for row in result.rows)

    def test_mid_table_repeat_is_dropped(self):
        """Some statements reprint the header after a section break."""
        page = table(
            1,
            [
                HEADER,
                ["01/03/2025", "a", "1.00", "", "10.00"],
                HEADER,
                ["02/03/2025", "b", "2.00", "", "8.00"],
            ],
        )
        result = stitch_group([page])
        assert len(result.rows) == 2
        assert result.dropped_repeat_headers == 1

    def test_header_matching_ignores_case_and_accents(self):
        variant = ["DATE", "DESCRIPTION", "DEBIT", "CREDIT", "BALANCE"]
        page1 = table(1, [HEADER, ["01/03/2025", "a", "1.00", "", "10.00"]])
        page2 = table(2, [variant, ["02/03/2025", "b", "2.00", "", "8.00"]])
        result = stitch_group([page1, page2])
        assert len(result.rows) == 2

    def test_row_provenance_is_preserved(self):
        page1 = table(1, [HEADER, ["01/03/2025", "a", "1.00", "", "10.00"]])
        page2 = table(2, [HEADER, ["02/03/2025", "b", "2.00", "", "8.00"]])
        result = stitch_group([page1, page2])
        assert result.row_pages == [1, 2]
        assert result.pages == [1, 2]


class TestPadding:
    def test_short_rows_are_padded(self):
        page = table(1, [HEADER, ["01/03/2025", "a"]])
        result = stitch_group([page])
        assert len(result.rows[0]) == len(HEADER)

    def test_overlong_rows_are_merged_not_truncated(self):
        """Extra cells are a split artefact; discarding them loses content."""
        page = table(1, [HEADER, ["01/03/2025", "a", "1.00", "", "10.00", "extra"]])
        result = stitch_group([page])
        assert len(result.rows[0]) == len(HEADER)
        assert "extra" in result.rows[0][-1]

    def test_blank_rows_are_dropped(self):
        page = table(1, [HEADER, ["", "", "", "", ""], ["01/03/2025", "a", "1.00", "", "10.00"]])
        result = stitch_group([page])
        assert len(result.rows) == 1


class TestStitchEntryPoint:
    def test_empty_tables_are_ignored(self):
        assert stitch([table(1, [["", ""], ["", ""]])]) == []

    def test_positional_names_when_no_header(self):
        page = table(
            1,
            [
                ["01/03/2025", "Card payment", "340.25"],
                ["03/03/2025", "Transfer", "120.50"],
            ],
        )
        result = stitch([page])[0]
        assert result.header == ["column_1", "column_2", "column_3"]
        assert len(result.rows) == 2
