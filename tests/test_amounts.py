"""Tests for the amount and date parsers.

This is the densest test file in the project on purpose. Everything here is a
real string observed in a real finance document, and every one of them has a
way of producing a plausible wrong number rather than an obvious failure.

The most important tests are the ones asserting ``None``. Returning ``None``
means the cell shows up in the validation report and gets looked at; returning
a wrong float means it lands in a client's accounts.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pdf2csv.core.amounts import (
    infer_dayfirst,
    infer_decimal_separator,
    is_blank_marker,
    looks_numeric,
    parse_amount,
    parse_date,
    repair_ocr_digits,
)


class TestPlainNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", 0.0),
            ("5", 5.0),
            ("12.50", 12.5),
            ("1234.56", 1234.56),
            ("1,234.56", 1234.56),
            ("1,234,567.89", 1234567.89),
            ("0.01", 0.01),
            (".5", 0.5),
        ],
    )
    def test_anglo(self, raw, expected):
        assert parse_amount(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.234,56", 1234.56),
            ("1.234.567,89", 1234567.89),
            ("12,50", 12.5),
            ("0,01", 0.01),
        ],
    )
    def test_continental(self, raw, expected):
        assert parse_amount(raw) == pytest.approx(expected)

    def test_space_grouping(self):
        """French typesetting groups thousands with spaces, including NBSP."""
        assert parse_amount("1 234,56") == pytest.approx(1234.56)
        assert parse_amount("1 234,56") == pytest.approx(1234.56)
        assert parse_amount("1 234,56") == pytest.approx(1234.56)


class TestNegatives:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("(1,234.56)", -1234.56),
            ("[1,234.56]", -1234.56),
            ("-1234.56", -1234.56),
            ("1234.56-", -1234.56),  # SAP and several core banking exports
            ("−1234.56", -1234.56),  # unicode minus
            ("–1234.56", -1234.56),  # en dash used as minus
        ],
    )
    def test_negative_notations(self, raw, expected):
        assert parse_amount(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,234.56 DR", -1234.56),
            ("1,234.56DB", -1234.56),
            ("1,234.56 CR", 1234.56),
            ("1,234.56CT", 1234.56),
            ("DR 1,234.56", -1234.56),
            ("CR 1,234.56", 1234.56),
        ],
    )
    def test_debit_credit_markers(self, raw, expected):
        assert parse_amount(raw) == pytest.approx(expected)

    def test_stacked_notation_still_means_debit(self):
        """Brackets and a DR marker both say "debit"; saying it twice is still
        a debit, not a credit. Also exercises stripping to a fixed point."""
        assert parse_amount("(1,234.56) DR") == pytest.approx(-1234.56)
        assert parse_amount("$(1,234.56)") == pytest.approx(-1234.56)

    def test_marker_lookalikes_in_words_are_not_signs(self):
        """"ADDR" ends in DR but is not an amount."""
        assert parse_amount("ADDR") is None


class TestCurrency:
    @pytest.mark.parametrize(
        "raw",
        ["$1,234.56", "1,234.56 USD", "USD 1,234.56", "£1,234.56", "€1,234.56", "1 234,56 TND"],
    )
    def test_symbols_and_codes_are_stripped(self, raw):
        assert abs(parse_amount(raw)) == pytest.approx(1234.56)

    def test_currency_with_negative(self):
        assert parse_amount("($1,234.56)") == pytest.approx(-1234.56)


class TestRejection:
    """Strings that must NOT become numbers."""

    @pytest.mark.parametrize(
        "raw",
        [
            "Page 1 of 12",  # naive digit-stripping yields 112
            "Account 40-12-88",
            "Description",
            "Balance b/f",
            "Ref 0041123 paid",
            "12 Main Street",
            "N/A",
            "",
            "   ",
            None,
        ],
    )
    def test_returns_none(self, raw):
        assert parse_amount(raw) is None

    def test_booleans_are_not_numbers(self):
        """bool is a subclass of int; True must not silently become 1.0."""
        assert parse_amount(True) is None

    def test_blank_markers(self):
        for marker in ["-", "--", "—", "nil", "N/A", "néant", "none"]:
            assert is_blank_marker(marker), marker
            assert parse_amount(marker) is None

    def test_ocr_noise_with_multiple_dots(self):
        assert parse_amount("1.2.3.4") is None


class TestExplicitSeparator:
    """A document-wide decision must override the per-value heuristic."""

    def test_comma_decimal_forced(self):
        assert parse_amount("1.234", ",") == pytest.approx(1234.0)
        assert parse_amount("12,50", ",") == pytest.approx(12.5)

    def test_dot_decimal_forced(self):
        assert parse_amount("1,234", ".") == pytest.approx(1234.0)
        assert parse_amount("12.50", ".") == pytest.approx(12.5)

    def test_ambiguous_value_resolved_differently_by_locale(self):
        """The whole reason document-wide inference exists."""
        assert parse_amount("1.234", ".") == pytest.approx(1.234)
        assert parse_amount("1.234", ",") == pytest.approx(1234.0)


class TestInferDecimalSeparator:
    def test_continental_document(self):
        assert infer_decimal_separator(["1.234,56", "890,10", "12,00"]) == ","

    def test_anglo_document(self):
        assert infer_decimal_separator(["1,234.56", "890.10", "12.00"]) == "."

    def test_grouping_only_implies_the_other(self):
        assert infer_decimal_separator(["1,234,567", "8,900"]) == "."
        assert infer_decimal_separator(["1.234.567", "8.900"]) == ","

    def test_no_evidence(self):
        assert infer_decimal_separator(["100", "250", "abc"]) is None

    def test_ignores_non_numeric(self):
        assert infer_decimal_separator(["Description", "Page 1 of 2", "1.234,56"]) == ","


class TestLooksNumeric:
    @pytest.mark.parametrize("raw", ["1,234.56", "(340.25)", "12", "1 234,56 TND"])
    def test_true(self, raw):
        assert looks_numeric(raw)

    @pytest.mark.parametrize("raw", ["Bloomberg LP", "Page 1 of 12", "", "-", "Ref 12A"])
    def test_false(self, raw):
        assert not looks_numeric(raw)


class TestOcrRepair:
    def test_repairs_digit_lookalikes_in_numbers(self):
        assert parse_amount(repair_ocr_digits("l,234.56")) == pytest.approx(1234.56)
        assert parse_amount(repair_ocr_digits("1,2S4.56")) == pytest.approx(1254.56)
        assert parse_amount(repair_ocr_digits("B90.10")) == pytest.approx(890.10)

    def test_leaves_isolated_letters_alone(self):
        """No digit nearby means no evidence this was ever a number."""
        assert repair_ocr_digits("Solar") == "Solar"
        assert repair_ocr_digits("OK") == "OK"

    def test_would_corrupt_text_if_misapplied(self):
        """Documents the danger: this is why callers gate it on numeric columns.

        The substitution is correct in a numeric cell and catastrophic in a
        description column, so normalize.py only ever calls it on columns it has
        already established are numeric, in rows that came from OCR.
        """
        assert repair_ocr_digits("B2B") == "828"


class TestDates:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2025-03-04", dt.date(2025, 3, 4)),
            ("2025/03/04", dt.date(2025, 3, 4)),
            ("04/03/2025", dt.date(2025, 3, 4)),
            ("04-03-2025", dt.date(2025, 3, 4)),
            ("04.03.2025", dt.date(2025, 3, 4)),
        ],
    )
    def test_dayfirst_default(self, raw, expected):
        assert parse_date(raw) == expected

    def test_monthfirst_when_told(self):
        assert parse_date("04/03/2025", dayfirst=False) == dt.date(2025, 4, 3)

    def test_unambiguous_beats_the_setting(self):
        """25 cannot be a month, whatever the locale hint says."""
        assert parse_date("25/03/2025", dayfirst=False) == dt.date(2025, 3, 25)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("4 Mar 2025", dt.date(2025, 3, 4)),
            ("4 March 2025", dt.date(2025, 3, 4)),
            ("Mar 4 2025", dt.date(2025, 3, 4)),
            ("4 mars 2025", dt.date(2025, 3, 4)),
            ("4 avril 2025", dt.date(2025, 4, 4)),
        ],
    )
    def test_named_months(self, raw, expected):
        assert parse_date(raw) == expected

    def test_two_digit_years(self):
        assert parse_date("04/03/25") == dt.date(2025, 3, 4)
        assert parse_date("04/03/98") == dt.date(1998, 3, 4)

    @pytest.mark.parametrize("raw", ["", None, "Description", "31/02/2025", "13/13/2025", "-"])
    def test_rejects(self, raw):
        assert parse_date(raw) is None


class TestInferDayfirst:
    def test_proof_from_a_high_first_component(self):
        assert infer_dayfirst(["25/03/2025", "04/03/2025"]) is True

    def test_proof_of_month_first(self):
        assert infer_dayfirst(["03/25/2025", "03/04/2025"]) is False

    def test_no_evidence(self):
        assert infer_dayfirst(["04/03/2025", "05/06/2025"]) is None

    def test_iso_carries_no_evidence(self):
        assert infer_dayfirst(["2025-03-04", "2025-06-05"]) is None


class TestConcatenatedDigits:
    """OCR runs adjacent cells together on dense tables.

    The result parses as a perfectly valid float in the 1e38 range, lands in a
    numeric column, and looks like nothing a reviewer expects — so it survives.
    """

    def test_an_absurdly_long_digit_run_is_not_an_amount(self):
        assert parse_amount("365274688988937798751190328024881") is None

    def test_amounts_up_to_a_representable_size_still_parse(self):
        assert parse_amount("999999999999.99") == pytest.approx(999999999999.99)
        assert parse_amount("1 234 567 890,12") == pytest.approx(1234567890.12)
