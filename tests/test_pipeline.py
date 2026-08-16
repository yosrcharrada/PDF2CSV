"""End-to-end tests against real PDF fixtures.

Expected values here are written out by hand from what the fixture documents
actually say, not captured from pipeline output. A golden file regenerated from
the code under test only proves the code still does whatever it did last time,
which is not the same as proving it is right.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from pdf2csv import run
from pdf2csv.core.export import export_result
from pdf2csv.models import PageKind

# What the ruled fixture states, read off the document.
RULED_COLUMNS = ["Date", "Description", "Reference", "Debit", "Credit", "Balance"]
RULED_DEBIT_TOTAL = 2201.95
RULED_CREDIT_TOTAL = 6362.85
RULED_CLOSING = 16610.90


class TestRuledStatement:
    def test_extracts_every_row_from_both_pages(self, ruled_statement):
        result = run(ruled_statement)
        assert len(result.dataframe) == 10, "the repeated header must not eat page 2"
        assert result.columns == RULED_COLUMNS

    def test_pages_are_classified_digital(self, ruled_statement):
        result = run(ruled_statement)
        assert result.meta.page_kinds == [PageKind.DIGITAL, PageKind.DIGITAL]
        assert result.meta.n_scanned == 0

    def test_amounts_reconcile_against_the_document(self, ruled_statement):
        frame = run(ruled_statement).dataframe
        assert frame["Debit"].sum() == pytest.approx(RULED_DEBIT_TOTAL)
        assert frame["Credit"].sum() == pytest.approx(RULED_CREDIT_TOTAL)
        assert frame["Balance"].iloc[-1] == pytest.approx(RULED_CLOSING)

    def test_dates_are_iso(self, ruled_statement):
        frame = run(ruled_statement).dataframe
        assert frame["Date"].iloc[0] == "2025-03-01"
        assert frame["Date"].iloc[-1] == "2025-03-31"

    def test_reference_keeps_its_leading_zeros(self, ruled_statement):
        frame = run(ruled_statement).dataframe
        assert frame["Reference"].iloc[1] == "0041123"
        assert frame["Reference"].dtype == object

    def test_totals_row_is_not_in_the_data(self, ruled_statement):
        frame = run(ruled_statement).dataframe
        assert not frame["Description"].str.contains("TOTAL", case=False, na=False).any()

    def test_all_checks_pass(self, ruled_statement):
        report = run(ruled_statement).report
        assert report.passed, report.summary()
        assert not report.failed_checks


class TestBorderlessFrenchStatement:
    def test_columns_are_not_merged(self, borderless_statement):
        """Letterhead text spans the Date/Libellé gap; it must not merge them."""
        result = run(borderless_statement)
        assert result.columns == ["Date", "Libellé", "Débit", "Crédit", "Solde"]

    def test_extracts_every_row(self, borderless_statement):
        assert len(run(borderless_statement).dataframe) == 7

    def test_page_footers_are_not_data_rows(self, borderless_statement):
        frame = run(borderless_statement).dataframe
        joined = " ".join(frame.astype(str).to_numpy().ravel().tolist())
        assert "Page 1" not in joined
        assert "Page 2" not in joined

    def test_european_decimals(self, borderless_statement):
        """1.245,00 is 1245.00, not 1.245."""
        frame = run(borderless_statement).dataframe
        assert frame["Débit"].dropna().tolist() == pytest.approx([189.90, 1245.00, 12.50])
        assert frame["Solde"].iloc[-1] == pytest.approx(14146.25)

    def test_accented_headers_survive(self, borderless_statement):
        assert "Libellé" in run(borderless_statement).columns

    def test_all_checks_pass(self, borderless_statement):
        report = run(borderless_statement).report
        assert report.passed, report.summary()


class TestBrokenStatement:
    """If this ever passes validation, the gate is broken and the rest is noise."""

    def test_validation_fails(self, broken_statement):
        assert not run(broken_statement).report.passed

    def test_the_missing_credit_is_identified(self, broken_statement):
        report = run(broken_statement).report
        totals = next(c for c in report.checks if c.id == "stated_totals")
        assert not totals.passed
        assert "3,150.00" in totals.detail

    def test_the_breaking_row_is_named(self, broken_statement):
        report = run(broken_statement).report
        balance = next(c for c in report.checks if c.id == "running_balance")
        assert not balance.passed
        assert "row 6" in balance.detail

    def test_the_csv_is_still_produced(self, broken_statement, tmp_path):
        """Failures annotate the export; they never block it."""
        result = run(broken_statement)
        paths = export_result(result, tmp_path / "broken.csv")
        assert paths.csv.is_file()
        assert len(pd.read_csv(paths.csv)) == 9


class TestNoTable:
    def test_prose_produces_a_clean_empty_result(self, letter):
        result = run(letter)
        assert result.dataframe.empty
        assert not result.report.passed
        assert any(c.id == "table_found" and not c.passed for c in result.report.checks)

    def test_prose_is_not_sent_to_ocr(self, letter):
        """A page with a text layer and no image has nothing OCR could add,
        and OCR-ing it costs a minute and invents a one-column table."""
        result = run(letter)
        assert result.meta.n_scanned == 0


class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run(tmp_path / "nope.pdf")

    def test_not_a_pdf(self, tmp_path):
        bogus = tmp_path / "not.pdf"
        bogus.write_text("this is not a PDF", encoding="utf-8")
        with pytest.raises(ValueError):
            run(bogus)


class TestProgress:
    def test_callback_receives_stages(self, ruled_statement):
        seen = []
        run(ruled_statement, progress=lambda stage, cur, total, msg: seen.append(stage))
        assert "routing" in seen
        assert "done" in seen


class TestExport:
    def test_writes_csv_sidecar_and_workbook(self, ruled_statement, tmp_path):
        result = run(ruled_statement)
        paths = export_result(result, tmp_path / "out.csv")

        assert paths.csv.is_file()
        assert paths.report_json.is_file()
        assert paths.xlsx is not None and paths.xlsx.is_file()

    def test_sidecar_records_the_verdict_and_the_source_hash(self, ruled_statement, tmp_path):
        """A CSV found on a share three weeks later still carries its proof."""
        result = run(ruled_statement)
        paths = export_result(result, tmp_path / "out.csv")

        sidecar = json.loads(paths.report_json.read_text(encoding="utf-8"))
        assert sidecar["validation"]["passed"] is True
        assert len(sidecar["document"]["sha256"]) == 64
        assert sidecar["n_rows"] == 10

    def test_csv_opens_correctly_in_excel(self, borderless_statement, tmp_path):
        """A BOM-less UTF-8 CSV shows "Débit" as "DÃ©bit" on a Windows double click."""
        result = run(borderless_statement)
        paths = export_result(result, tmp_path / "fr.csv")

        assert paths.csv.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "Libellé" in pd.read_csv(paths.csv, encoding="utf-8-sig").columns

    def test_never_writes_a_csv_without_its_report(self, ruled_statement, tmp_path):
        result = run(ruled_statement)
        paths = export_result(result, tmp_path / "out.csv", write_xlsx=False)
        assert paths.report_json.is_file()


class TestCaching:
    def test_same_file_hashes_identically(self, ruled_statement):
        assert run(ruled_statement).meta.sha256 == run(ruled_statement).meta.sha256
