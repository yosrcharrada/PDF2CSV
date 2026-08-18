"""Five facts from a declaration → the 22-field standard row.

Pure and deterministic: the same facts always produce the same row. ISIN is the
single exception — it is pool-allocated, carries state, and is injected by the
caller rather than derived here. Keeping it out means every other field can be
unit-tested with no fixtures, no files and no ordering.

Anything in this module that is a business decision rather than a computation
is marked with the evidence behind it. Several were ambiguous in the source
mapping document and are recorded here so a future reader can see what was
decided and on what basis, instead of finding a bare constant.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

__all__ = [
    "COLUMNS",
    "ISSUERS",
    "DeclarationFacts",
    "Issuer",
    "build_name",
    "classify_type",
    "format_rate",
    "isin_group_key",
    "issuer_from_title",
    "reconcile",
    "to_row",
]


# --------------------------------------------------------------------------- #
# Issuer registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Issuer:
    """One issuing company, keyed by the token that appears in the PDF title."""

    short: str
    name: str
    issuer_code: str
    """Field 3."""
    bic: str
    """Field 17."""


# The SWIFT table in the source mapping document has its code column sorted
# alphabetically while the name column keeps the original order, so only two
# rows — BTK Leasing and Tunisie Leasing — happen to line up. Taken literally it
# gives CIL the code BHLSTN00020.
#
# Rebuilt here on the prefix rule instead: the four-letter code prefix is the
# company's own identifier, so CIL maps to CILTTN00020. Confirmed against the
# reference declaration.
ISSUERS: dict[str, Issuer] = {
    "AIL": Issuer("AIL", "BTK LEASING EMETTEUR CD", "AILETN00003", "AILETN00020"),
    "ATT": Issuer("ATT", "ATTIJARI LEASING CD", "ATTLTN00003", "ATTLTN00020"),
    # Ends 00004 where every other issuer ends 00003. Transcribed as printed;
    # flagged in docs/DECLARATIONS.md as needing confirmation, because a slip
    # here corrupts every BH Leasing row and nothing else would reveal it.
    "BHL": Issuer("BHL", "BH LEASING SA EMETTEUR CD", "BHLSTN00004", "BHLSTN00020"),
    "CIL": Issuer(
        "CIL",
        "COMPAGNIE INTERNATIONALE DE LEASING EMETTEUR CD",
        "CILTTN00003",
        "CILTTN00020",
    ),
    "HNL": Issuer("HNL", "HANNIBAL LEASE SA EMETTEUR CD", "HNLETN00003", "HNLETN00020"),
    "UNF": Issuer("UNF", "UNION DE FACTORING EMETTEUR CD", "UNFATN00003", "UNFATN00020"),
    "TLF": Issuer(
        "TLF", "TUNISIE LEASING ET FACTORING EMETTEUR CD", "TLFTTN00003", "TLFTTN00020"
    ),
    "ATL": Issuer("ATL", "ARAB TUNISIAN LEASE EMETTEUR CD", "ATLETN00003", "ATLETN00020"),
}

# Only CIL is confirmed against a real document. The other seven tokens are
# inferred from their code prefixes, which is a reasonable guess and not a
# verified fact — see docs/DECLARATIONS.md.
CONFIRMED_TOKENS = frozenset({"CIL"})


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

NOMINAL_PER_CERTIFICATE = 500
"""Field 19 is 500 x quantité. Fixed, and deliberately *not* derived from the
prix unitaire printed on the document, which is 500 000,000 on the reference
case. Confirmed."""

DISCOUNT_MAX_DAYS = 365

TYPE_DISCOUNT = "Discount"
TYPE_COUPON = "Coupon"

# Two literal strings that go into a downstream system. "deport" is almost
# certainly a typo for "dépôt" in the source document, and is reproduced exactly
# because if the receiving system string-matches, correcting the spelling breaks
# every Coupon row while looking like an improvement. Flagged for confirmation.
INSTRUMENT_DISCOUNT = "certificat de depot inf 1an TF"
INSTRUMENT_COUPON = "certificat de deport sup 1an TF"

AUCTION_TYPE = "Standard"
CLIENT = "NO"
"""Fixed until client creation exists. The entire FICHE DU SOUSCRIPTEUR block —
name, nationality, identifier, address, balance — is therefore unused."""

COLUMNS = [
    "ISIN",
    "name",
    "issuer",
    "rate",
    "totalNumberOfCertificates",
    "totalAmountToBePaid",
    "auctionDate",
    "type",
    "issueDate",
    "startDate",
    "maturityDate",
    "issuanceProgramme",
    "instrument",
    "guarantor",
    "entitlementDate",
    "auctionType",
    "BIC",
    "code",
    "nominal",
    "quantity",
    "amountToBePaid",
    "client",
]


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #


@dataclass
class DeclarationFacts:
    """Everything read off the PDF. The only OCR-dependent input to the mapping.

    ``prix_unitaire`` and ``montant`` are used for reconciliation only and never
    reach the output row, which is why they are optional: a document that omits
    them still produces a complete row, just with one fewer check behind it.
    """

    title: str
    taux: float
    """Percent, not a fraction: 8.0 means 8,00%."""
    quantite: int
    date_souscription: dt.date
    date_remboursement: dt.date

    prix_unitaire: float | None = None
    montant: float | None = None

    source_page: int = 1
    libelle: str = ""
    confidence: float = 1.0
    """Lowest OCR confidence among the cells these facts came from."""


# --------------------------------------------------------------------------- #
# Derivations
# --------------------------------------------------------------------------- #


def issuer_from_title(title: str) -> Issuer:
    """Find the issuing company from the declaration title.

    The token follows the word DECLARATION, so that is where it is looked for
    first. A word-boundary match cannot be relied on: the recogniser routinely
    closes up the spaces and returns ``DECLARATIONCIL49-2026``, in which ``CIL``
    is preceded by a letter and no boundary exists.

    A plain substring search would mis-attribute a row the moment one token
    appeared inside another company's name, so the fallback accepts a match only
    when exactly one token is present. An ambiguous title raises rather than
    guesses: the issuer decides both the issuer code and the BIC, and a wrong
    one is invisible in the output.
    """
    upper = re.sub(r"[^A-Z0-9]+", "", title.upper())

    after = re.search(r"DECLARATION([A-Z]+)", upper)
    if after:
        leading = after.group(1)
        for length in (4, 3):
            candidate = leading[:length]
            if candidate in ISSUERS:
                return ISSUERS[candidate]

    present = [short for short in ISSUERS if short in upper]
    if len(present) == 1:
        return ISSUERS[present[0]]
    if len(present) > 1:
        raise ValueError(
            f"Title {title!r} matches several issuers "
            f"({', '.join(sorted(present))}); it cannot be attributed automatically."
        )

    raise ValueError(
        f"No known issuer token in title {title!r}. "
        f"Known tokens: {', '.join(sorted(ISSUERS))}."
    )


def format_rate(taux: float) -> str:
    """Field 4, as the reference files write it: ``8`` and ``8,4``.

    French decimal comma, trailing zeros trimmed. Not ``8.0``: the receiving
    system is fed a semicolon-delimited file written in a comma-decimal locale,
    and a full stop there is a different number.
    """
    text = f"{taux:.4f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def format_taux(taux: float) -> str:
    """``8.0`` → ``'8,00%'`` — two decimals, comma separator, French convention."""
    return f"{taux:.2f}".replace(".", ",") + "%"


def build_name(issuer: Issuer, taux: float, remboursement: dt.date) -> str:
    """Field 2. ``'CIL 8,00% 09092026'``"""
    return f"{issuer.short} {format_taux(taux)} {remboursement.strftime('%d%m%Y')}"


def classify_type(souscription: dt.date, remboursement: dt.date) -> str:
    """Field 8. A year or less is a Discount; longer carries a coupon."""
    return (
        TYPE_DISCOUNT
        if (remboursement - souscription).days <= DISCOUNT_MAX_DAYS
        else TYPE_COUPON
    )


def instrument_for(type_: str) -> str:
    return INSTRUMENT_DISCOUNT if type_ == TYPE_DISCOUNT else INSTRUMENT_COUPON


def to_row(facts: DeclarationFacts, isin: str) -> dict[str, object]:
    """Build the standard row. ``isin`` comes from the pool allocator."""
    issuer = issuer_from_title(facts.title)
    type_ = classify_type(facts.date_souscription, facts.date_remboursement)

    def fr(value: dt.date) -> str:
        return value.strftime("%d/%m/%Y")

    # "Date actuel" in the source document resolves to the date de souscription,
    # not the date the file happens to be processed. That distinction is what
    # makes the mapping a pure function: reprocessing a declaration next month
    # must produce a byte-identical row, and a processing-date reading would
    # quietly produce a different one.
    reference = facts.date_souscription

    return {
        "ISIN": isin,
        "name": build_name(issuer, facts.taux, facts.date_remboursement),
        "issuer": issuer.issuer_code,
        "rate": format_rate(facts.taux),
        "totalNumberOfCertificates": facts.quantite,
        "totalAmountToBePaid": 0,
        "auctionDate": fr(reference),
        "type": type_,
        "issueDate": fr(reference),
        "startDate": fr(reference),
        "maturityDate": fr(facts.date_remboursement),
        "issuanceProgramme": "",
        "instrument": instrument_for(type_),
        "guarantor": "",
        "entitlementDate": fr(facts.date_souscription),
        "auctionType": AUCTION_TYPE,
        "BIC": issuer.bic,
        "code": "",
        "nominal": NOMINAL_PER_CERTIFICATE * facts.quantite,
        "quantity": facts.quantite,
        "amountToBePaid": 0,
        "client": CLIENT,
    }


# --------------------------------------------------------------------------- #
# Grouping and reconciliation
# --------------------------------------------------------------------------- #


def isin_group_key(facts: DeclarationFacts) -> tuple:
    """Rows sharing this key are one instrument and share a single ISIN.

    The source document phrases the rule as "if the date de souscription equals
    the date de remboursement", which cannot be comparing those two fields
    within one row — they are never equal on a real certificate. Read as: the
    same issuer, rate and pair of dates across several subscribers describes one
    issuance split between them, so it consumes one code rather than several.
    """
    return (
        issuer_from_title(facts.title).short,
        round(facts.taux, 4),
        facts.date_souscription,
        facts.date_remboursement,
    )


def reconcile(facts: DeclarationFacts) -> list[dict[str, object]]:
    """Arithmetic checks against the document itself.

    Deliberately checks the figures the mapping does *not* use. Prix unitaire
    and montant never reach the output row, which makes them independent
    evidence that the quantité was read correctly — the one fact with no other
    corroboration and which scales the nominal.
    """
    checks: list[dict[str, object]] = []

    if facts.prix_unitaire is not None and facts.montant is not None:
        computed = facts.prix_unitaire * facts.quantite
        checks.append(
            {
                "id": "montant",
                "title": "Prix unitaire x quantite equals the montant printed",
                "passed": abs(computed - facts.montant) <= 0.001,
                "detail": f"{computed:,.3f} vs {facts.montant:,.3f} printed",
            }
        )

    ordered = facts.date_remboursement > facts.date_souscription
    checks.append(
        {
            "id": "date_order",
            "title": "Repayment falls after subscription",
            "passed": ordered,
            "detail": f"{facts.date_souscription} then {facts.date_remboursement}",
        }
    )

    checks.append(
        {
            "id": "taux_range",
            "title": "The rate is within a plausible range",
            "passed": 0 < facts.taux < 100,
            "detail": f"{format_taux(facts.taux)}",
        }
    )

    checks.append(
        {
            "id": "quantite_positive",
            "title": "The quantity is a positive whole number",
            "passed": facts.quantite > 0,
            "detail": str(facts.quantite),
        }
    )

    return checks
