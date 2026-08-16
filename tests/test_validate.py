"""Tests for the reconciliation checks.

Two properties are asserted throughout, and they matter more than any
individual check:

* A check that cannot find its inputs **does not run**, rather than failing.
  A report full of failures that only mean "this document does not have a
  balance column" trains people to ignore the report.
* A check that fails **names the row**. "The totals do not reconcile" sends an
  analyst to a 400-row CSV with no starting point.
"""

from __future__ import annotations

import pytest

from pdf2csv.core.normalize import normalize_table
from pdf2csv.core.stitch import StitchedTable
from pdf2csv.core.validate import run_all
from pdf2csv.models import DocumentMeta, PageKind, Severity, ValidationReport
from pdf2csv.profiles import GENERIC, Profile


def build(header, rows, *, profile=GENERIC, kinds=None):
    table = StitchedTable(
        header=list(header),
        rows=[list(r) for r in rows],
        row_pages=[1] * len(rows),
        row_kinds=kinds or [PageKind.DIGITAL] * len(rows),
    )
    report = ValidationReport()
    normalised = normalize_table(table, report, profile=profile)
    meta = DocumentMeta(n_pages=1, page_kinds=[PageKind.DIGITAL])
    run_all(normalised, report, meta, profile)
    return normalised, report


def check(report, check_id):
    for item in report.checks:
        if item.id == check_id:
            return item
    return None


STATEMENT_HEADER = ["Date", "Description", "Debit", "Credit", "Balance"]
STATEMENT_ROWS = [
    ["01/03/2025", "Opening balance", "", "", "1,000.00"],
    ["02/03/2025", "Payment", "100.00", "", "900.00"],
    ["03/03/2025", "Receipt", "", "250.00", "1,150.00"],
    ["04/03/2025", "Payment", "50.00", "", "1,100.00"],
]


class TestRunningBalance:
    def test_consistent_ledger_passes(self):
        _, report = build(STATEMENT_HEADER, STATEMENT_ROWS)
        result = check(report, "running_balance")
        assert result is not None and result.passed

    def test_a_broken_row_is_named(self):
        rows = [list(r) for r in STATEMENT_ROWS]
        rows[2][4] = "1,999.00"  # balance no longer follows
        _, report = build(STATEMENT_HEADER, rows)

        result = check(report, "running_balance")
        assert result is not None and not result.passed
        assert "row 3" in result.detail

    def test_a_dropped_row_is_caught(self):
        """The failure this project exists to catch."""
        rows = [STATEMENT_ROWS[0], STATEMENT_ROWS[1], STATEMENT_ROWS[3]]
        _, report = build(STATEMENT_HEADER, rows)
        assert not check(report, "running_balance").passed

    def test_reversed_sign_convention_is_detected_not_failed(self):
        """Some ledgers have debits increase the balance. That is not an error."""
        header = ["Date", "Description", "Debit", "Credit", "Balance"]
        rows = [
            ["01/03/2025", "Opening", "", "", "1,000.00"],
            ["02/03/2025", "a", "100.00", "", "1,100.00"],
            ["03/03/2025", "b", "", "250.00", "850.00"],
            ["04/03/2025", "c", "50.00", "", "900.00"],
        ]
        _, report = build(header, rows)
        assert check(report, "running_balance").passed

    def test_skipped_when_there_is_no_balance_column(self):
        _, report = build(
            ["Date", "Description", "Amount"],
            [["01/03/2025", "a", "1.00"], ["02/03/2025", "b", "2.00"], ["03/03/2025", "c", "3.00"]],
        )
        assert check(report, "running_balance") is None


class TestStatedTotals:
    def test_matching_totals_pass(self):
        rows = [*STATEMENT_ROWS, ["", "TOTAL", "150.00", "250.00", "1,100.00"]]
        _, report = build(STATEMENT_HEADER, rows)
        result = check(report, "stated_totals")
        assert result is not None and result.passed

    def test_mismatch_reports_the_difference(self):
        rows = [*STATEMENT_ROWS, ["", "TOTAL", "150.00", "999.00", "1,100.00"]]
        _, report = build(STATEMENT_HEADER, rows)

        result = check(report, "stated_totals")
        assert not result.passed
        assert "Credit" in result.detail
        assert result.severity is Severity.ERROR

    def test_balance_column_compares_closing_not_sum(self):
        """A totals row states the closing balance, not the sum of balances.

        Summing every intermediate balance and comparing that to the closing
        figure would fail on every correctly extracted statement ever produced.
        """
        rows = [*STATEMENT_ROWS, ["", "TOTAL", "150.00", "250.00", "1,100.00"]]
        normalised, report = build(STATEMENT_HEADER, rows)

        assert normalised.stated_totals["Balance"] == pytest.approx(1100.0)
        assert normalised.frame["Balance"].sum() == pytest.approx(4150.0)
        assert check(report, "stated_totals").passed

    def test_skipped_when_the_document_states_no_totals(self):
        _, report = build(STATEMENT_HEADER, STATEMENT_ROWS)
        assert check(report, "stated_totals") is None


class TestNumericParsing:
    def test_unreadable_cell_fails_and_is_named(self):
        # Corrupt one cell in a column that stays numeric overall. A column
        # where most values fail is not a numeric column at all, and is
        # correctly left as text instead.
        rows = [list(r) for r in STATEMENT_ROWS]
        rows[1][4] = "1O0,OO?"
        _, report = build(STATEMENT_HEADER, rows)

        result = check(report, "numeric_parsing")
        assert not result.passed
        assert "row 2" in result.detail

    def test_a_mostly_unreadable_column_is_treated_as_text(self):
        rows = [list(r) for r in STATEMENT_ROWS]
        for row in rows:
            row[4] = "see statement"
        _, report = build(STATEMENT_HEADER, rows)
        assert check(report, "numeric_parsing").passed

    def test_nil_markers_are_not_parse_failures(self):
        """A bare dash means nil. Reporting it trains people to ignore the report."""
        rows = [list(r) for r in STATEMENT_ROWS]
        rows[1][3] = "-"
        _, report = build(STATEMENT_HEADER, rows)
        assert check(report, "numeric_parsing").passed


class TestDebitsEqualCredits:
    def test_balanced_journal_passes(self):
        profile = Profile(name="journal", balance_columns={"debit": "Debit", "credit": "Credit"})
        rows = [["a", "100.00", ""], ["b", "", "60.00"], ["c", "", "40.00"]]
        _, report = build(["Entry", "Debit", "Credit"], rows, profile=profile)
        assert check(report, "debits_equal_credits").passed

    def test_unbalanced_journal_fails(self):
        profile = Profile(name="journal", balance_columns={"debit": "Debit", "credit": "Credit"})
        rows = [["a", "100.00", ""], ["b", "", "60.00"], ["c", "", "30.00"]]
        _, report = build(["Entry", "Debit", "Credit"], rows, profile=profile)
        assert not check(report, "debits_equal_credits").passed

    def test_not_applied_to_bank_statements(self):
        """A statement's debits and credits are not meant to be equal."""
        _, report = build(STATEMENT_HEADER, STATEMENT_ROWS)
        assert check(report, "debits_equal_credits") is None


class TestDuplicatesAndCoverage:
    def test_duplicate_rows_warn_but_do_not_fail_the_report(self):
        rows = [*STATEMENT_ROWS, STATEMENT_ROWS[1]]
        _, report = build(STATEMENT_HEADER, rows)
        result = check(report, "duplicate_rows")
        assert not result.passed
        assert result.severity is Severity.WARNING

    def test_warnings_do_not_sink_the_overall_result(self):
        """Otherwise "review this cell" and "your totals are wrong" look alike.

        Uses a ledger with no balance column, so the duplicated row raises the
        duplicate warning without also breaking the running balance.
        """
        header = ["Date", "Description", "Amount"]
        rows = [
            ["01/03/2025", "a", "100.00"],
            ["02/03/2025", "b", "250.00"],
            ["03/03/2025", "c", "50.00"],
            ["02/03/2025", "b", "250.00"],
        ]
        _, report = build(header, rows)

        assert not check(report, "duplicate_rows").passed
        assert report.passed is True


class TestExpectedColumns:
    def test_missing_column_fails(self):
        profile = Profile(name="t", expected_columns=["Date", "Description", "Balance", "Fee"])
        _, report = build(STATEMENT_HEADER, STATEMENT_ROWS, profile=profile)
        result = check(report, "expected_columns")
        assert not result.passed
        assert "Fee" in result.detail

    def test_skipped_when_a_profile_declares_none(self):
        _, report = build(STATEMENT_HEADER, STATEMENT_ROWS)
        assert check(report, "expected_columns") is None


class TestReportSemantics:
    def test_every_failed_check_carries_a_hint(self):
        """The analyst is not an engineer. A failure must say what to do."""
        rows = [*STATEMENT_ROWS, ["", "TOTAL", "150.00", "999.00", "1,100.00"]]
        _, report = build(STATEMENT_HEADER, rows)
        for item in report.failed_checks:
            assert item.hint.strip(), f"{item.id} failed without a hint"

    def test_checks_never_mention_code(self):
        _, report = build(STATEMENT_HEADER, STATEMENT_ROWS)
        for item in report.checks:
            text = f"{item.title} {item.hint}"
            assert "None" not in text
            assert "()" not in text
            assert "_" not in item.title
