"""Tests for column typing, OCR repair gating, and totals-row separation."""

from __future__ import annotations

import pandas as pd
import pytest

from pdf2csv.core.normalize import AMOUNT, TEXT, clean_headers, normalize_table
from pdf2csv.core.stitch import StitchedTable
from pdf2csv.models import PageKind, Severity, ValidationReport
from pdf2csv.profiles import Profile


def make_table(header, rows, *, kinds=None, confidences=None) -> StitchedTable:
    return StitchedTable(
        header=list(header),
        rows=[list(r) for r in rows],
        row_pages=[1] * len(rows),
        row_kinds=kinds or [PageKind.DIGITAL] * len(rows),
        row_confidences=confidences,
    )


class TestCleanHeaders:
    def test_blank_headers_get_positional_names(self):
        assert clean_headers(["Date", "", "Amount"]) == ["Date", "column_2", "Amount"]

    def test_duplicates_are_disambiguated(self):
        assert clean_headers(["Amount", "Amount"]) == ["Amount", "Amount (2)"]

    def test_whitespace_is_collapsed_but_wording_preserved(self):
        """Original wording survives: the analyst reads the CSV next to the PDF."""
        assert clean_headers(["  Débit   (TND) "]) == ["Débit (TND)"]


class TestColumnTyping:
    def test_amounts_become_floats(self):
        table = make_table(
            ["Date", "Description", "Amount"],
            [
                ["01/03/2025", "a", "1,234.56"],
                ["02/03/2025", "b", "(340.25)"],
                ["03/03/2025", "c", "12.00"],
            ],
        )
        result = normalize_table(table, ValidationReport())
        assert result.frame["Amount"].tolist() == pytest.approx([1234.56, -340.25, 12.0])
        assert result.amount_columns == ["Amount"]

    def test_dates_become_iso_strings(self):
        table = make_table(
            ["Date", "Amount"],
            [["01/03/2025", "1.00"], ["25/03/2025", "2.00"], ["31/03/2025", "3.00"]],
        )
        result = normalize_table(table, ValidationReport())
        assert result.frame["Date"].tolist() == ["2025-03-01", "2025-03-25", "2025-03-31"]

    def test_mixed_column_stays_text(self):
        table = make_table(
            ["Note"], [["1,234.56"], ["see overleaf"], ["n/a but check"], ["various"]]
        )
        result = normalize_table(table, ValidationReport())
        assert result.columns[0].kind == TEXT


class TestIdentifierColumns:
    def test_leading_zeros_keep_the_column_as_text(self):
        """0041123 must not become 41123 — the reference stops matching."""
        table = make_table(
            ["Reference", "Amount"],
            [["0041123", "1.00"], ["0041187", "2.00"], ["0041234", "3.00"]],
        )
        result = normalize_table(table, ValidationReport())
        assert result.columns[0].kind == TEXT
        assert result.frame["Reference"].tolist() == ["0041123", "0041187", "0041234"]

    def test_long_digit_runs_with_an_id_header_stay_text(self):
        """A 16-digit card number must not arrive in Excel as 1.23457E+15."""
        table = make_table(
            ["Account number", "Amount"],
            [
                ["1234567812345678", "1.00"],
                ["1234567812345679", "2.00"],
                ["1234567812345670", "3.00"],
            ],
        )
        result = normalize_table(table, ValidationReport())
        assert result.columns[0].kind == TEXT

    def test_profile_can_force_text(self):
        profile = Profile(name="t", identifier_columns=[r"\bcode\b"])
        table = make_table(["Code", "Amount"], [["12345", "1.00"], ["23456", "2.00"], ["34567", "3.00"]])
        result = normalize_table(table, ValidationReport(), profile=profile)
        assert result.columns[0].kind == TEXT

    def test_ordinary_amounts_are_not_mistaken_for_identifiers(self):
        table = make_table(
            ["Amount"], [["1,234.56"], ["890.10"], ["12.00"], ["45,000.00"]]
        )
        result = normalize_table(table, ValidationReport())
        assert result.columns[0].kind == AMOUNT


class TestTotalsRows:
    def test_totals_row_is_lifted_out_of_the_data(self):
        table = make_table(
            ["Description", "Amount"],
            [["a", "100.00"], ["b", "200.00"], ["TOTAL", "300.00"]],
        )
        result = normalize_table(table, ValidationReport())

        assert len(result.frame) == 2, "the totals row must not stay in the data"
        assert result.stated_totals["Amount"] == pytest.approx(300.0)
        assert result.total_row_count == 1

    def test_a_transaction_mentioning_total_is_not_a_totals_row(self):
        """"Total fees debited in March" is a transaction, not a totals row.

        Pulling it out would silently delete a row from the analyst's CSV.
        """
        table = make_table(
            ["Description", "Amount"],
            [
                ["Total fees debited during the March billing period", "25.00"],
                ["b", "200.00"],
                ["c", "75.00"],
            ],
        )
        result = normalize_table(table, ValidationReport())
        assert len(result.frame) == 3
        assert not result.stated_totals

    def test_french_totals_label(self):
        profile = Profile(name="fr", total_row_labels=["total"])
        table = make_table(
            ["Libellé", "Montant"],
            [["a", "100,00"], ["b", "200,00"], ["Total des mouvements", "300,00"]],
        )
        result = normalize_table(table, ValidationReport(), profile=profile)
        assert len(result.frame) == 2
        assert result.stated_totals["Montant"] == pytest.approx(300.0)


class TestOcrRepairGating:
    def test_repairs_a_numeric_cell_from_a_scanned_row(self):
        table = make_table(
            ["Amount"],
            [["1,234.56"], ["89O.10"], ["12.00"], ["45.00"]],
            kinds=[PageKind.SCANNED] * 4,
        )
        report = ValidationReport()
        result = normalize_table(table, report)

        assert result.frame["Amount"].tolist() == pytest.approx([1234.56, 890.10, 12.0, 45.0])
        assert result.repaired_cells == 1
        assert any("corrected" in f.reason for f in report.flags)

    def test_does_not_repair_digital_rows(self):
        """A letter inside a number on a digital page is really there."""
        table = make_table(
            ["Amount"],
            [["1,234.56"], ["89O.10"], ["12.00"], ["45.00"]],
            kinds=[PageKind.DIGITAL] * 4,
        )
        report = ValidationReport()
        result = normalize_table(table, report)

        assert result.repaired_cells == 0
        assert pd.isna(result.frame["Amount"].iloc[1])
        assert any(f.reason == "could not be read as a number" for f in report.flags)

    def test_does_not_touch_text_columns(self):
        table = make_table(
            ["Description", "Amount"],
            [["Bloomberg LP", "1.00"], ["Solar Ltd", "2.00"], ["Boston Inc", "3.00"]],
            kinds=[PageKind.SCANNED] * 3,
        )
        result = normalize_table(table, ValidationReport())
        assert result.frame["Description"].tolist() == ["Bloomberg LP", "Solar Ltd", "Boston Inc"]

    def test_low_confidence_numeric_cells_are_flagged(self):
        table = make_table(
            ["Amount"],
            [["1,234.56"], ["890.10"], ["12.00"]],
            kinds=[PageKind.SCANNED] * 3,
            confidences=[[0.99], [0.42], [0.98]],
        )
        report = ValidationReport()
        normalize_table(table, report, low_confidence=0.80)

        low = [f for f in report.flags if "confidence" in f.reason]
        assert len(low) == 1
        assert low[0].row == 1
        assert low[0].severity is Severity.WARNING


class TestLocaleInference:
    def test_continental_document_parses_consistently(self):
        table = make_table(
            ["Montant"], [["1.234,56"], ["890,10"], ["12,00"], ["45.000,00"]]
        )
        result = normalize_table(table, ValidationReport())
        assert result.frame["Montant"].tolist() == pytest.approx([1234.56, 890.10, 12.0, 45000.0])

    def test_profile_overrides_inference(self):
        profile = Profile(name="t", decimal_separator=",")
        table = make_table(["Montant"], [["1.234"], ["2.500"], ["3.750"]])
        result = normalize_table(table, ValidationReport(), profile=profile)
        assert result.frame["Montant"].tolist() == pytest.approx([1234.0, 2500.0, 3750.0])


class TestMergedCellFill:
    def test_off_by_default(self):
        table = make_table(["Client", "Amount"], [["ACME", "1.00"], ["", "2.00"], ["", "3.00"]])
        result = normalize_table(table, ValidationReport())
        assert result.frame["Client"].tolist() == ["ACME", "", ""]

    def test_forward_fills_when_a_profile_asks(self):
        profile = Profile(name="t", fill_merged_labels=True)
        table = make_table(["Client", "Amount"], [["ACME", "1.00"], ["", "2.00"], ["", "3.00"]])
        result = normalize_table(table, ValidationReport(), profile=profile)
        assert result.frame["Client"].tolist() == ["ACME", "ACME", "ACME"]

    def test_never_fills_numeric_columns(self):
        """Forward-filling an amount fabricates money that reconciles perfectly."""
        profile = Profile(name="t", fill_merged_labels=True)
        table = make_table(
            ["Client", "Amount"], [["ACME", "1.00"], ["", ""], ["", "3.00"]]
        )
        result = normalize_table(table, ValidationReport(), profile=profile)
        assert pd.isna(result.frame["Amount"].iloc[1])
