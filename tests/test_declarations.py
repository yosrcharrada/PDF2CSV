"""Declaration → standard row.

No client documents are used here, and none are committed. The mapping tests
are pure functions of five values, and the extraction tests are built from
synthetic text boxes that reproduce the geometry observed on a real scan —
including the three things that actually broke it: a heading split across two
lines, two headings merged into one recognised box, and a value glued to its
neighbour with no separator.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from pdf2csv.declarations.facts import PageReading, build_anchors, extract_from_boxes
from pdf2csv.declarations.mapping import (
    ALL_COLUMNS,
    COLUMNS,
    SOURCE_COLUMNS,
    DeclarationFacts,
    build_name,
    certificate_count,
    classify_type,
    format_amount,
    format_taux,
    isin_group_key,
    issuer_from_title,
    reconcile,
    to_row,
)
from pdf2csv.models import TextBox

# The reference declaration, as printed.
REFERENCE = DeclarationFacts(
    title="DECLARATION CIL 49-2026",
    taux=8.0,
    quantite=10,
    date_souscription=dt.date(2026, 7, 31),
    date_remboursement=dt.date(2026, 9, 9),
    prix_unitaire=500000.000,
    montant=5000000.000,
)

EXPECTED = {
    "ISIN": "TNEDPD3F01J5",
    "name": "CIL 8,00% 09092026",
    "issuer": "CILTTN00003",
    "rate": "8",   # not 8.0 -- the reference files write "8" and "8,4"
    "totalnumberOfCertificates": 10,
    "totalAmountToBePaid": "0",
    "auctionDate": "31/07/2026",
    "type": "Discount",
    "issueDate": "31/07/2026",
    "startDate": "31/07/2026",
    "maturityDate": "09/09/2026",
    "issuanceProgramme": "",
    # "1an", not "1 an" -- both reference CSVs from the finance team agree,
    # and the spec transcribed it with a space.
    "instrument": "certificat de depot inf 1an TF",
    "guarantor": "",
    "entitlementDate": "31/07/2026",
    "auctionType": "Standard",
    "BIC": "CILTTN00020",
    "code": "",
    # 500 000 a certificate, which is also the montant the document prints.
    # Checked against all four reference rows -- CIL's ten certificates are
    # 5 000 000, and BTK Leasing's seven, two and ten are 3 500 000, 1 000 000
    # and 5 000 000.
    "nominalValueAllotted": "5000000",
    "numberOfCertificates": 10,
    "amountToBePaid": "0",
    # As the BTK Leasing reference file spells it. The CIL file writes "no".
    "client": "No",
    # Columns 23-36: always written, always empty, filled by the analyst.
    "clientId": "",
    "clientType": "",
    "firstName": "",
    "lastName": "",
    "registrationDate": "",
    "economicSector": "",
    "residentStatus": "",
    "nationality": "",
    "defaultAssetCategory": "",
    "natureOfIdentification": "",
    "nationalId": "",
    "fiscalId": "",
    "gender": "",
    "investorType": "",
}


class TestReferenceCase:
    def test_every_field_matches(self):
        row = to_row(REFERENCE, isin="TNEDPD3F01J5")
        assert {key: row[key] for key in COLUMNS} == EXPECTED

    def test_column_order_is_fixed(self):
        """The CSV is consumed by another system; column order is part of the
        contract, not a presentation detail."""
        assert list(to_row(REFERENCE, isin="X"))[:36] == COLUMNS
        assert len(COLUMNS) == 36

    def test_the_standard_columns_come_first_and_alone(self):
        """The source columns are appended, never interleaved.

        Whatever imports this reads the first 36 by position, so a source
        column appearing among them would shift every field after it."""
        row = to_row(REFERENCE, isin="X")
        assert list(row) == ALL_COLUMNS
        assert not set(SOURCE_COLUMNS) & set(COLUMNS)

    def test_the_document_own_figures_are_kept(self):
        """A value read and then dropped is unauditable: nobody looking at the
        CSV can tell what the paper said. The taux and the montant are folded
        into derived fields, so they are also carried as printed."""
        row = to_row(REFERENCE, isin="X")
        assert row["Taux"] == "8,00%"
        assert row["Montant"] == "5000000"
        assert row["Quantite"] == "10"
        assert row["Date de remboursement"] == "09/09/2026"

    def test_a_column_the_document_lacks_is_present_and_empty(self):
        """Every row has the same shape whichever reader produced it, so a
        declaration still carries the fiche's columns, blank."""
        row = to_row(REFERENCE, isin="X")
        assert row["Interet brut"] == ""
        assert row["Libelle"] == ""
        assert all(column in row for column in SOURCE_COLUMNS)

    def test_reconciliation_passes(self):
        assert all(c["passed"] for c in reconcile(REFERENCE))


class TestIssuerLookup:
    def test_spaced_title(self):
        assert issuer_from_title("DECLARATION CIL 49-2026").bic == "CILTTN00020"

    def test_title_with_the_spaces_closed_up(self):
        """What the recogniser actually returns from a scan."""
        assert issuer_from_title("DECLARATIONCIL49-2026").bic == "CILTTN00020"

    def test_bic_uses_the_prefix_rule_not_the_sorted_swift_list(self):
        """The source document's SWIFT column was sorted independently of its
        name column, which pairs CIL with BHLSTN00020. The prefix rule is the
        correct pairing and this test pins it."""
        assert issuer_from_title("DECLARATION CIL 1-2026").bic == "CILTTN00020"
        assert issuer_from_title("DECLARATION CIL 1-2026").bic != "BHLSTN00020"

    def test_unknown_issuer_raises_rather_than_guessing(self):
        with pytest.raises(ValueError, match="No known issuer"):
            issuer_from_title("DECLARATION XYZ 1-2026")

    @pytest.mark.parametrize(
        ("token", "code"),
        [("AIL", "AILETN00003"), ("ATT", "ATTLTN00003"), ("HNL", "HNLETN00003")],
    )
    def test_other_issuers(self, token, code):
        assert issuer_from_title(f"DECLARATION {token} 1-2026").issuer_code == code


class TestDerivedFields:
    def test_rate_uses_a_comma_decimal_with_trailing_zeros_trimmed(self):
        from pdf2csv.declarations.mapping import format_rate
        assert format_rate(8.0) == "8"
        assert format_rate(8.4) == "8,4"
        assert format_rate(7.84) == "7,84"

    def test_taux_formatting_is_french(self):
        assert format_taux(8.0) == "8,00%"
        assert format_taux(7.845) == "7,84%" or format_taux(7.845) == "7,85%"

    def test_name_format(self):
        issuer = issuer_from_title("DECLARATION CIL 1-2026")
        assert build_name(issuer, 8.0, dt.date(2026, 9, 9)) == "CIL 8,00% 09092026"

    def test_a_year_or_less_is_a_discount(self):
        assert classify_type(dt.date(2026, 7, 31), dt.date(2026, 9, 9)) == "Discount"
        assert classify_type(dt.date(2026, 1, 1), dt.date(2026, 12, 31)) == "Discount"

    def test_longer_than_a_year_carries_a_coupon(self):
        assert classify_type(dt.date(2026, 1, 1), dt.date(2027, 6, 1)) == "Coupon"

    def test_the_365_day_boundary(self):
        start = dt.date(2026, 1, 1)
        assert classify_type(start, start + dt.timedelta(days=365)) == "Discount"
        assert classify_type(start, start + dt.timedelta(days=366)) == "Coupon"

    def test_instrument_follows_the_type(self):
        coupon = DeclarationFacts(
            title="DECLARATION CIL 1-2026",
            taux=8.0,
            quantite=1,
            date_souscription=dt.date(2026, 1, 1),
            date_remboursement=dt.date(2028, 1, 1),
        )
        assert to_row(coupon, "X")["instrument"] == "certificat de deport sup 1an TF"

    def test_nominal_is_the_montant_the_document_prints(self):
        """Ten certificates at 500 000 each. Both reference files agree, and an
        earlier reading of 500 a certificate was out by a factor of a thousand
        on every row it produced."""
        assert to_row(REFERENCE, "X")["nominalValueAllotted"] == "5000000"

    def test_certificates_come_from_the_montant_not_the_printed_quantity(self):
        """The BTK Leasing fiche prints a quantité of 5 against a montant of
        3 500 000 at 500 000 apiece, and its reference row says seven. Where
        the two disagree the montant is what the finance team followed."""
        inconsistent = replace(REFERENCE, quantite=5, montant=3500000.0)
        assert certificate_count(inconsistent) == 7
        assert to_row(inconsistent, "X")["numberOfCertificates"] == 7

    def test_amounts_are_written_with_a_comma(self):
        """The file is semicolon-delimited for a comma-decimal locale, where a
        full stop is a different number. The reference writes 4924922,296."""
        assert format_amount(4924922.296) == "4924922,296"
        assert format_amount(3500000.0) == "3500000"

    def test_dates_all_come_from_the_subscription_date(self):
        """"Date actuel" is the subscription date, not the processing date.
        Otherwise reprocessing the same file next month yields a different row."""
        row = to_row(REFERENCE, "X")
        for field in ("auctionDate", "issueDate", "startDate", "entitlementDate"):
            assert row[field] == "31/07/2026"

    def test_the_mapping_is_a_pure_function(self):
        assert to_row(REFERENCE, "X") == to_row(REFERENCE, "X")


class TestReconciliation:
    def test_a_quantity_disagreeing_with_the_montant_is_reported(self):
        """The check's whole purpose: the mapping counts certificates from the
        montant, so the printed quantité is the only independent evidence that
        figure was read correctly, and a disagreement has to be visible."""
        wrong = DeclarationFacts(
            title="DECLARATION CIL 49-2026",
            taux=8.0,
            quantite=100,  # misread; should be 10
            date_souscription=dt.date(2026, 7, 31),
            date_remboursement=dt.date(2026, 9, 9),
            prix_unitaire=500000.000,
            montant=5000000.000,
        )
        checks = {c["id"]: c["passed"] for c in reconcile(wrong)}
        assert checks["quantite_matches_montant"] is False

    def test_reversed_dates_are_caught(self):
        reversed_ = DeclarationFacts(
            title="DECLARATION CIL 1-2026",
            taux=8.0,
            quantite=1,
            date_souscription=dt.date(2026, 9, 9),
            date_remboursement=dt.date(2026, 7, 31),
        )
        checks = {c["id"]: c["passed"] for c in reconcile(reversed_)}
        assert checks["date_order"] is False

    def test_checks_still_run_without_the_optional_figures(self):
        minimal = DeclarationFacts(
            title="DECLARATION CIL 1-2026",
            taux=8.0,
            quantite=1,
            date_souscription=dt.date(2026, 1, 1),
            date_remboursement=dt.date(2026, 6, 1),
        )
        assert all(c["passed"] for c in reconcile(minimal))


class TestIsinGrouping:
    def test_same_instrument_shares_a_key(self):
        other = DeclarationFacts(
            title="DECLARATION CIL 50-2026",  # different subscriber, same issuance
            taux=8.0,
            quantite=25,
            date_souscription=dt.date(2026, 7, 31),
            date_remboursement=dt.date(2026, 9, 9),
        )
        assert isin_group_key(REFERENCE) == isin_group_key(other)

    def test_a_different_rate_is_a_different_instrument(self):
        other = DeclarationFacts(
            title="DECLARATION CIL 50-2026",
            taux=7.5,
            quantite=10,
            date_souscription=dt.date(2026, 7, 31),
            date_remboursement=dt.date(2026, 9, 9),
        )
        assert isin_group_key(REFERENCE) != isin_group_key(other)


# --------------------------------------------------------------------------- #
# Extraction from recognised text
# --------------------------------------------------------------------------- #


def box(text, x0, y0, x1, y1, conf=0.98):
    return TextBox(text=text, x0=x0, y0=y0, x1=x1, y1=y1, confidence=conf)


def reference_page() -> PageReading:
    """The real scan's geometry, reproduced.

    Coordinates and the exact recognised strings are taken from the reference
    declaration read at 200 DPI, including its three awkward features:
    ``Date de`` / ``souscription`` split over two lines, ``Taux Prix unitaire``
    merged into one box over two columns, and the rate glued to a date as
    ``31/07/20268,00%``.
    """
    return PageReading(
        page_number=1,
        rotation=270,
        width=2337,
        height=1650,
        boxes=[
            box("DECLARATIONCIL49-2026", 920, 299, 1409, 336),
            # headings
            box("Libelle du Certificat de depot", 147, 769, 589, 809),
            box("Taux Prix unitaire", 680, 772, 966, 810),
            box("Montant", 1007, 772, 1141, 810),
            box("Quantite", 1203, 773, 1338, 810),
            box("Date de", 1432, 775, 1556, 811),
            box("Date de", 1729, 775, 1853, 811),
            box("souscription", 1399, 830, 1589, 861),
            box("remboursement", 1670, 830, 1913, 861),
            # values
            box("CIL-CertificatdeDepot8,00%", 69, 892, 466, 927),
            box("31/07/20268,00%", 510, 895, 764, 927),
            box("500000,000", 792, 896, 952, 928),
            box("5000000,000", 985, 895, 1167, 930),
            box("10", 1252, 896, 1291, 930),
            box("31/07/2026", 1416, 896, 1575, 932),
            box("09/09/2026", 1713, 896, 1869, 933),
        ],
    )


class TestAnchors:
    def test_a_heading_split_over_two_lines_is_found(self):
        anchors = build_anchors(reference_page().boxes)
        assert "date_souscription" in anchors
        assert "date_remboursement" in anchors

    def test_two_headings_merged_into_one_box_are_separated(self):
        """``Taux Prix unitaire`` is one recognised box covering two columns.
        Without splitting it, both headings claim the whole span and each reads
        whichever value sits a pixel higher."""
        anchors = build_anchors(reference_page().boxes)
        taux, prix = anchors["taux"], anchors["prix_unitaire"]
        assert taux.x1 <= prix.x0 + 1, "the two columns must not overlap"


class TestExtraction:
    def test_reads_all_five_facts(self):
        facts = extract_from_boxes(reference_page())
        assert facts is not None
        assert facts.taux == pytest.approx(8.0)
        assert facts.quantite == 10
        assert facts.date_souscription == dt.date(2026, 7, 31)
        assert facts.date_remboursement == dt.date(2026, 9, 9)

    def test_the_rate_is_not_taken_from_the_date_glued_to_it(self):
        """``31/07/20268,00%`` yields 268,00 to a naive percentage match."""
        facts = extract_from_boxes(reference_page())
        assert facts is not None
        assert facts.taux == pytest.approx(8.0)

    def test_prix_unitaire_is_not_the_montant(self):
        facts = extract_from_boxes(reference_page())
        assert facts is not None
        assert facts.prix_unitaire == pytest.approx(500000.0)
        assert facts.montant == pytest.approx(5000000.0)

    def test_dinar_amounts_use_comma_decimals(self):
        facts = extract_from_boxes(reference_page())
        assert facts is not None
        assert facts.montant == pytest.approx(5000000.0)  # not 5.0

    def test_the_extracted_facts_produce_the_reference_row(self):
        """End to end from recognised text, without any PDF."""
        facts = extract_from_boxes(reference_page())
        assert facts is not None
        row = to_row(facts, "TNEDPD3F01J5")
        assert {key: row[key] for key in COLUMNS} == EXPECTED

    def test_a_page_without_a_declaration_returns_none(self):
        blank = PageReading(page_number=1, rotation=0, width=2337, height=1650,
                            boxes=[box("Some unrelated letter", 10, 10, 400, 50)])
        assert extract_from_boxes(blank) is None

    def test_an_incomplete_page_returns_none_rather_than_a_partial_row(self):
        """A wrong rotation parses to nothing, which is how it gets rejected."""
        page = reference_page()
        page.boxes = [b for b in page.boxes if b.text != "10"]
        assert extract_from_boxes(page) is None


class TestReferenceHeader:
    """The header is a contract with whatever imports this file."""

    def test_matches_the_finance_team_header_exactly(self):
        expected = (
            "ISIN;name;issuer;rate;totalnumberOfCertificates;totalAmountToBePaid;"
            "auctionDate;type;issueDate;startDate;maturityDate;issuanceProgramme;"
            "instrument;guarantor;entitlementDate;auctionType;BIC;code;"
            "nominalValueAllotted;numberOfCertificates;amountToBePaid;client;"
            "clientId;clientType;firstName;lastName;registrationDate;economicSector;"
            "residentStatus;nationality;defaultAssetCategory;natureOfIdentification;"
            "nationalId;fiscalId;gender;investorType"
        )
        assert ";".join(COLUMNS) == expected

    def test_the_subscriber_block_is_present_and_empty(self):
        """Written on every row so each export has the same shape, and left
        blank because client is fixed at "no" and the analyst fills them in."""
        row = to_row(REFERENCE, "X")
        for column in COLUMNS[22:]:
            assert column in row
            assert row[column] == ""
