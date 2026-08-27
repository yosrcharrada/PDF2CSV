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
from dataclasses import dataclass, field

__all__ = [
    "ALL_COLUMNS",
    "COLUMNS",
    "ISSUERS",
    "SOURCE_COLUMNS",
    "DeclarationFacts",
    "GroupTotals",
    "Issuer",
    "Subscriber",
    "amount_to_be_paid",
    "build_name",
    "certificate_count",
    "classify_type",
    "format_amount",
    "format_rate",
    "isin_group_key",
    "issuer_from_title",
    "nominal_value",
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
    aliases: tuple[str, ...] = ()
    """Other tokens the same company is printed under.

    The registry is keyed on the three letters that open the issuer code, but a
    document does not have to use them. BTK Leasing writes itself ``BTKL`` in a
    fiche libelle while its code begins ``AILE``, and nothing derives one from
    the other, so the spelling has to be recorded rather than computed."""

    @property
    def label(self) -> str:
        """The token field 2 is written with.

        The reference row for BTK Leasing reads ``BTKL CD 8,40% 31072027`` --
        the market abbreviation, not the code prefix. Where a company has no
        separate abbreviation this is simply the short token, which is what the
        CIL reference row uses."""
        return self.aliases[0] if self.aliases else self.short


# The SWIFT table in the source mapping document has its code column sorted
# alphabetically while the name column keeps the original order, so only two
# rows — BTK Leasing and Tunisie Leasing — happen to line up. Taken literally it
# gives CIL the code BHLSTN00020.
#
# Rebuilt here on the prefix rule instead: the four-letter code prefix is the
# company's own identifier, so CIL maps to CILTTN00020. Confirmed against the
# reference declaration.
ISSUERS: dict[str, Issuer] = {
    # `BTKL` is how this company writes itself in a fiche libelle and in the
    # reference row's `name`; `AILE` is only ever the code prefix. Confirmed
    # against the BTK Leasing reference file.
    "AIL": Issuer(
        "AIL", "BTK LEASING EMETTEUR CD", "AILETN00003", "AILETN00020", ("BTKL",)
    ),
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
CONFIRMED_TOKENS = frozenset({"CIL", "AIL"})


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

NOMINAL_PER_CERTIFICATE = 500_000
"""Face value of one certificate, and the fallback for field 19.

Corrected from 500 after checking all four reference rows: CIL shows 5 000 000
against ten certificates, and BTK Leasing shows 3 500 000, 1 000 000 and
5 000 000 against seven, two and ten. Every one of them is 500 000 a
certificate, which is also the prix unitaire the documents print.

It is only the fallback. Where the document states a montant, field 19 is that
montant -- see :func:`nominal_value`."""

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
CLIENT = "No"
# Spelled as the BTK Leasing reference file spells it. The CIL file writes
# "no" and this writes neither in upper case, which matched neither. Which of
# the two the receiving system wants is still open -- see docs/DECLARATIONS.md.
"""Fixed until client creation exists. The entire FICHE DU SOUSCRIPTEUR block —
name, nationality, identifier, address, balance — is therefore unused."""

# Columns 23-36 describe the subscriber. They are always written and always
# empty: `client` is fixed at "no", so nothing here is derived, and the analyst
# fills them in afterwards. Emitting them empty rather than omitting them keeps
# every export the same shape, which is what a downstream import needs.
CLIENT_COLUMNS = [
    "clientId",
    "clientType",
    "firstName",
    "lastName",
    "registrationDate",
    "economicSector",
    "residentStatus",
    "nationality",
    "defaultAssetCategory",
    "natureOfIdentification",
    "nationalId",
    "fiscalId",
    "gender",
    "investorType",
]

# The 36 columns of the finance team's reference files, in their order and
# with their exact spelling -- including "totalnumberOfCertificates" with a
# lower-case n. Both reference files agree on every name, so these are copied
# rather than guessed. Column order and spelling are part of the contract with
# whatever imports this, not a presentation choice.
COLUMNS = [
    "ISIN",
    "name",
    "issuer",
    "rate",
    "totalnumberOfCertificates",
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
    "nominalValueAllotted",
    "numberOfCertificates",
    "amountToBePaid",
    "client",
    *CLIENT_COLUMNS,
]

assert len(COLUMNS) == 36

# Columns the source document prints that the standard layout has no home for.
#
# Field 2 folds the instrument and the rate into one string -- the reference
# files write "BTKL CD 8,40% 31072027" -- and fields 19 to 21 are derived, not
# transcribed. Read literally that means a document can state a libelle, a
# taux, a montant and a quantite and have none of them appear anywhere in the
# output as the document wrote them. Anything read and then dropped is
# unauditable: nobody looking at the CSV can tell what the paper said.
#
# So they are appended, after the 36 and never among them. The first 36 columns
# stay byte-identical to the reference files, which is the part that is a
# contract with the receiving system; these carry what the document actually
# printed. A column stays empty where its document has no such column, rather
# than being omitted, so every row of every kind has the same shape.
#
# Spelled without accents, following the reference files' own
# "certificat de depot inf 1an TF".
SOURCE_COLUMNS = [
    "Libelle",
    "Taux",
    "Prix unitaire",
    "Montant",
    "Quantite",
    "Date de souscription",
    "Date de remboursement",
    "Nombre de jours",
    "Interet brut",
    "Retenue a la source",
    "Interet net",
    "Montant net",
    "Adresse",
    "Restriction",
]

ALL_COLUMNS = [*COLUMNS, *SOURCE_COLUMNS]
"""What is actually written: the contract, then the evidence for it."""


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Subscriber:
    """Who subscribed, as printed.

    A declaration names no subscriber; a fiche does, in a table of its own.
    Only the fields with somewhere to go in the standard layout are kept --
    the address and restriction columns are read and have no column to go to.
    """

    name: str = ""
    client_type: str = ""
    nationality: str = ""
    nature_of_identification: str = ""
    national_id: str = ""
    address: str = ""

    @property
    def is_entity(self) -> bool:
        return "MORALE" in self.client_type.upper()

    @property
    def given_name(self) -> str:
        """First word of a natural person's name; empty for a company.

        A legal entity has no forename to split off, and splitting one out of
        ``UNION FINANCIERE EXEMPLE SICAV`` would produce a firstName of
        ``UNION``.
        """
        if self.is_entity or not self.name.strip():
            return ""
        return self.name.split()[0]

    @property
    def family_name(self) -> str:
        """Everything after the forename, or the whole name for a company."""
        if self.is_entity:
            return self.name.strip()
        parts = self.name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else self.name.strip()


@dataclass(frozen=True)
class GroupTotals:
    """Fields 5 and 6, summed over every row that shares one ISIN."""

    certificates: int
    amount_to_be_paid: float


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

    document_date: dt.date | None = None
    """The date the document itself is dated -- "Fait a Tunis le ...".

    Distinct from the date de souscription, and the reference files prove the
    distinction matters: on the BTK Leasing fiche two rows are subscribed on
    31/07 and one on 03/08, yet all three carry auctionDate 03/08, the date the
    fiche was drawn. On the CIL declaration the two dates coincide, which is
    why a souscription reading looked correct there."""

    subscriber: Subscriber | None = None
    """Identity of the subscriber, where the document carries it."""

    extras: dict[str, str] = field(default_factory=dict)
    """Source columns as printed, keyed by their heading in ``SOURCE_COLUMNS``.

    Only for columns that cannot be derived from the facts above -- the
    interest columns of a fiche, its address and restriction. Everything else
    is filled in by :func:`to_row` from the facts themselves, so a document
    that states fewer columns simply leaves those blank."""

    source_page: int = 1
    page_count: int = 1
    """Pages in the source document.

    Carried on the facts because a row is no longer a page: a fiche puts
    several subscribers on one page and splits its columns across two, so
    counting rows would report the wrong figure to whoever is reading the
    summary."""
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

    # Longest token first. `BTKL` must be tested before `BTK` would be, or a
    # company whose alias merely starts with another's would be shadowed.
    tokens = dict(ISSUERS)
    for issuer in ISSUERS.values():
        for alias in issuer.aliases:
            tokens[alias] = issuer
    ordered = sorted(tokens, key=len, reverse=True)

    after = re.search(r"DECLARATION([A-Z0-9]+)", upper)
    if after:
        leading = after.group(1)
        for token in ordered:
            if leading.startswith(token):
                return tokens[token]

    present = {tokens[t].short for t in ordered if t in upper}
    if len(present) == 1:
        return ISSUERS[present.pop()]
    if len(present) > 1:
        raise ValueError(
            f"Title {title!r} matches several issuers "
            f"({', '.join(sorted(present))}); it cannot be attributed automatically."
        )

    raise ValueError(
        f"No known issuer token in title {title!r}. "
        f"Known tokens: {', '.join(sorted(tokens))}."
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


def build_name(issuer: Issuer, taux: float, remboursement: dt.date, tag: str = "") -> str:
    """Field 2. ``'CIL 8,00% 09092026'``, or ``'BTKL CD 8,40% 31072027'``.

    The tag is present exactly when the source document names the instrument.
    A fiche libelle reads ``SER BTKL 8.40% CD 31072026`` and its reference row
    keeps the ``CD``; the CIL declaration title names no instrument and its
    reference row has none. Deriving the tag from the document rather than
    always writing or always omitting it is what reproduces both.
    """
    parts = [issuer.label, tag, format_taux(taux), remboursement.strftime("%d%m%Y")]
    return " ".join(part for part in parts if part)


def classify_type(souscription: dt.date, remboursement: dt.date) -> str:
    """Field 8. A year or less is a Discount; longer carries a coupon."""
    return (
        TYPE_DISCOUNT
        if (remboursement - souscription).days <= DISCOUNT_MAX_DAYS
        else TYPE_COUPON
    )


def instrument_for(type_: str) -> str:
    return INSTRUMENT_DISCOUNT if type_ == TYPE_DISCOUNT else INSTRUMENT_COUPON


def certificate_count(facts: DeclarationFacts) -> int:
    """Field 20, and the basis of fields 5 and 19.

    Taken from montant / prix unitaire rather than from the printed quantite,
    because that is what every reference row agrees with and the quantite does
    not. The BTK Leasing fiche prints quantite 5 against a montant of 3 500 000
    at 500 000 apiece, and the reference row for it says seven certificates.

    The quantite is not discarded -- :func:`reconcile` compares the two and
    raises a check when they disagree, which is how that row gets noticed
    rather than silently overridden.
    """
    if facts.montant and facts.prix_unitaire:
        exact = facts.montant / facts.prix_unitaire
        count = round(exact)
        if count > 0 and abs(exact - count) < 1e-6:
            return count
    return facts.quantite


def nominal_value(facts: DeclarationFacts) -> float:
    """Field 19: the montant as printed, or the face value if none was read."""
    if facts.montant:
        return float(facts.montant)
    return float(NOMINAL_PER_CERTIFICATE * certificate_count(facts))


def format_amount(value: float | None) -> str:
    """Money, in the comma-decimal convention the reference files are written in.

    ``4924922,296`` and ``3500000`` both appear in them, so a whole number is
    written without decimals and a fractional one keeps up to three. Written as
    text rather than a number because the file is semicolon-delimited for a
    comma-decimal locale, where a full stop is a different quantity.
    """
    if value is None:
        return ""
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 5e-4:
        return str(round(rounded))
    return f"{rounded:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def amount_to_be_paid(facts: DeclarationFacts) -> float:
    """Field 21. Zero unless the subscriber settles away from the issuer.

    Three of the four reference rows are zero, and the fourth carries the
    montant net. What separates them is field 18: the two zero BTK rows and the
    zero CIL row all hold their securities with the issuing company itself,
    while the row with a figure sits at another bank entirely (its code begins
    ``TN62UBCI``, its BIC is UBCI's).

    Field 18 is not printed anywhere in these documents, so the custodian is
    unknowable from the PDF and this returns zero -- correct for three of the
    four rows, and flagged on every run rather than assumed.
    """
    return 0.0


def to_row(
    facts: DeclarationFacts,
    isin: str,
    *,
    totals: GroupTotals | None = None,
    tag: str = "",
) -> dict[str, object]:
    """Build the standard row. ``isin`` comes from the pool allocator.

    ``totals`` carries fields 5 and 6 for the whole issuance when several
    subscribers share one ISIN. Omitted, they are taken from this row alone,
    which is the correct answer for a single-subscriber declaration.
    """
    issuer = issuer_from_title(facts.title)
    type_ = classify_type(facts.date_souscription, facts.date_remboursement)
    count = certificate_count(facts)
    paid = amount_to_be_paid(facts)
    totals = totals or GroupTotals(certificates=count, amount_to_be_paid=paid)

    def fr(value: dt.date) -> str:
        return value.strftime("%d/%m/%Y")

    # Fields 7, 9 and 10 are the date the document was drawn, not the date the
    # subscription was taken and not the date of processing. The BTK Leasing
    # fiche settles it: two of its rows are subscribed on 31/07 and one on
    # 03/08, and all three carry 03/08 -- the date on the fiche. A document
    # that does not state its own date falls back to the souscription, which is
    # what the CIL declaration does and where the two coincide anyway.
    drawn = facts.document_date or facts.date_souscription

    subscriber = facts.subscriber or Subscriber()

    return {
        "ISIN": isin,
        "name": build_name(issuer, facts.taux, facts.date_remboursement, tag),
        "issuer": issuer.issuer_code,
        "rate": format_rate(facts.taux),
        "totalnumberOfCertificates": totals.certificates,
        "totalAmountToBePaid": format_amount(totals.amount_to_be_paid),
        "auctionDate": fr(drawn),
        "type": type_,
        "issueDate": fr(drawn),
        "startDate": fr(drawn),
        "maturityDate": fr(facts.date_remboursement),
        "issuanceProgramme": "",
        "instrument": instrument_for(type_),
        "guarantor": "",
        # Field 15 is per subscriber, not per document: it is the date this
        # subscription took effect, which is why it stays on the souscription
        # while 7, 9 and 10 move to the date the document was drawn.
        "entitlementDate": fr(facts.date_souscription),
        "auctionType": AUCTION_TYPE,
        "BIC": issuer.bic,
        "code": "",
        "nominalValueAllotted": format_amount(nominal_value(facts)),
        "numberOfCertificates": count,
        "amountToBePaid": format_amount(paid),
        "client": CLIENT,
        # Columns 23-36. Written from the document where it names a subscriber,
        # and left blank where it does not -- a declaration never does.
        "clientId": "",
        "clientType": subscriber.client_type,
        "firstName": subscriber.given_name,
        "lastName": subscriber.family_name,
        "registrationDate": "",
        "economicSector": "",
        "residentStatus": "",
        "nationality": subscriber.nationality,
        "defaultAssetCategory": "",
        "natureOfIdentification": subscriber.nature_of_identification,
        "nationalId": subscriber.national_id,
        "fiscalId": "",
        "gender": "",
        "investorType": "",
        # Columns 37 onward: what the document printed, so the derived values
        # above can be checked against it without going back to the PDF.
        **_source_columns(facts),
    }


def _source_columns(facts: DeclarationFacts) -> dict[str, str]:
    """The document's own columns, derived where possible and carried where not.

    Deriving the common ones from the facts rather than requiring every reader
    to supply them means a declaration -- which prints a taux, a montant and a
    quantite but no libelle -- fills in what it has and leaves the rest blank,
    without the reader knowing anything about this list.
    """
    def money(value: float | None) -> str:
        return format_amount(value) if value is not None else ""

    derived = {
        "Libelle": facts.libelle,
        "Taux": format_taux(facts.taux),
        "Prix unitaire": money(facts.prix_unitaire),
        "Montant": money(facts.montant),
        # The quantite *as printed*, which is not always the number of
        # certificates: the BTK Leasing fiche prints 5 where the montant makes
        # 7. Keeping both is the whole point of these columns.
        "Quantite": str(facts.quantite),
        "Date de souscription": facts.date_souscription.strftime("%d/%m/%Y"),
        "Date de remboursement": facts.date_remboursement.strftime("%d/%m/%Y"),
    }
    return {
        column: str(facts.extras.get(column, derived.get(column, "")) or "")
        for column in SOURCE_COLUMNS
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

    The most valuable of these compares the printed quantité against the count
    the mapping actually uses, which comes from montant / prix unitaire. On the
    BTK Leasing fiche those disagree — it prints a quantité of 5 beside a
    montant of 3 500 000 at 500 000 each, and the reference row says seven.
    The document is internally inconsistent, so the check exists to surface
    that rather than to let either reading win quietly.
    """
    checks: list[dict[str, object]] = []

    if facts.prix_unitaire is not None and facts.montant is not None:
        counted = certificate_count(facts)
        checks.append(
            {
                "id": "quantite_matches_montant",
                "title": "The printed quantite agrees with montant / prix unitaire",
                "passed": counted == facts.quantite,
                "detail": (
                    f"{facts.montant:,.3f} / {facts.prix_unitaire:,.3f} = {counted}, "
                    f"quantite printed as {facts.quantite}"
                ),
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
