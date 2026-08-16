"""Documents that are not one table.

An annual report, a rate card or an accessibility guide carries several
unrelated tables of different shapes. Two things went wrong on real documents
of this class, and both are guarded here:

* Tables were **fused** because they had the same column count, producing one
  incoherent CSV whose header belonged to whichever table came first and whose
  totals meant nothing.
* Everything except the largest table was **silently discarded**, so most of
  the document vanished without the analyst being told.
"""

from __future__ import annotations

import pytest

from pdf2csv import run
from pdf2csv.core.stitch import group_tables
from pdf2csv.models import ExtractedTable, PageKind


@pytest.fixture
def multi_table(pdf_dir):
    path = pdf_dir / "multi_table.pdf"
    if not path.is_file():
        pytest.skip("fixture missing — run tests/fixtures/make_fixtures.py")
    return path


def table(page, rows, *, bbox=None, page_height=842.0):
    return ExtractedTable(
        page_number=page,
        kind=PageKind.DIGITAL,
        rows=rows,
        extractor="test",
        bbox=bbox,
        page_height=page_height,
    )


HEAD_A = ["Department", "2024", "2025"]
HEAD_B = ["Site", "2024", "2025"]
ROW = ["x", "1", "2"]


class TestGroupingRules:
    def test_two_tables_on_one_page_never_merge(self):
        """A single ruled grid comes back as one table object.

        If the extractor returned two, they are two tables — re-merging them
        overrides a decision made with far better information.
        """
        tables = [table(1, [HEAD_A, ROW]), table(1, [HEAD_B, ROW])]
        assert len(group_tables(tables)) == 2

    def test_same_width_mid_page_tables_on_consecutive_pages_do_not_merge(self):
        """Geometry says neither ran out of room, so neither continues."""
        mid_page = (40.0, 300.0, 400.0, 400.0)
        tables = [
            table(1, [HEAD_A, ROW], bbox=mid_page),
            table(2, [HEAD_B, ROW], bbox=mid_page),
        ]
        assert len(group_tables(tables)) == 2

    def test_a_table_that_runs_off_the_page_bottom_does_continue(self):
        ends_low = (40.0, 500.0, 400.0, 800.0)
        starts_high = (40.0, 60.0, 400.0, 300.0)
        tables = [
            table(1, [HEAD_A, ROW], bbox=ends_low),
            table(2, [ROW, ROW], bbox=starts_high),
        ]
        assert len(group_tables(tables)) == 1

    def test_a_repeated_header_continues_even_without_the_geometry(self):
        """A short table can legitimately continue without filling its page.

        The repeated column titles are the unambiguous marker, so they are
        honoured on their own.
        """
        mid_page = (40.0, 300.0, 400.0, 400.0)
        tables = [
            table(1, [HEAD_A, ROW], bbox=mid_page),
            table(2, [HEAD_A, ROW], bbox=mid_page),
        ]
        assert len(group_tables(tables)) == 1

    def test_without_geometry_it_falls_back_to_page_adjacency(self):
        tables = [table(1, [HEAD_A, ROW]), table(2, [ROW, ROW])]
        assert len(group_tables(tables)) == 1


class TestMultiTableDocument:
    def test_every_table_is_returned(self, multi_table):
        result = run(multi_table)
        assert len(result.tables_out) == 3, [t.label() for t in result.tables_out]

    def test_the_same_width_tables_stay_separate(self, multi_table):
        """Table A and Table B are both three columns and about different things."""
        result = run(multi_table)
        headers = {tuple(t.columns) for t in result.tables_out}

        assert ("Department", "2024", "2025") in headers
        assert ("Site", "2024", "2025") in headers

    def test_each_table_carries_its_own_report(self, multi_table):
        result = run(multi_table)
        for entry in result.tables_out:
            assert entry.report.checks, f"{entry.label()} was never validated"

    def test_each_table_has_a_findable_label(self, multi_table):
        """"Department, 2024" can be found in the PDF; "Table 2" cannot."""
        result = run(multi_table)
        labels = [t.label() for t in result.tables_out]
        assert all(labels)
        assert any("Department" in label for label in labels)

    def test_pages_are_recorded_for_each_table(self, multi_table):
        result = run(multi_table)
        for entry in result.tables_out:
            assert entry.pages == [1]

    def test_the_primary_is_still_the_first_entry(self, multi_table):
        result = run(multi_table)
        assert result.dataframe is result.tables_out[0].frame
        assert result.report is result.tables_out[0].report

    def test_secondary_tables_are_exported_too(self, multi_table, tmp_path):
        from pdf2csv.core.export import export_result

        result = run(multi_table)
        paths = export_result(result, tmp_path / "pack.csv")
        assert len(paths.extras) == len(result.tables_out) - 1
        for extra in paths.extras:
            assert extra.is_file()


class TestSingleTableIsUnaffected:
    """The common case must not pay for the multi-table one."""

    def test_a_statement_still_produces_exactly_one_table(self, ruled_statement):
        result = run(ruled_statement)
        assert len(result.tables_out) == 1
        assert len(result.dataframe) == 10

    def test_a_two_page_statement_is_still_stitched_into_one(self, borderless_statement):
        result = run(borderless_statement)
        assert len(result.tables_out) == 1
        assert result.tables_out[0].pages == [1, 2]
