"""Certificat de dépôt declarations → the standard 22-field row.

A different problem from the rest of this package, and worth stating plainly.

``pdf2csv.core`` answers "what table is in this PDF?". These documents are not
that. Their structure is fixed and known in advance, and the CSV is not a
transcription of the table — it is a *derived* row in a schema the document
never mentions. Only five facts come out of the PDF:

    title token, taux, quantité, date de souscription, date de remboursement

Everything else is constant, looked up in a registry, or computed. Two further
values, prix unitaire and montant, are read for reconciliation only.

That inversion is the whole reason this subpackage exists rather than being a
document profile. A profile describes how to read a table; here there is
almost no table-reading to do, and instead a body of business rules that must
be exact, versioned and unit-tested on their own.

The practical consequence is a much smaller accuracy surface. Getting a rate,
an integer and two dates right off a 200 DPI scan is a far easier promise than
reconstructing every cell of a table correctly.
"""

from pdf2csv.declarations.mapping import (
    COLUMNS,
    ISSUERS,
    DeclarationFacts,
    Issuer,
    build_name,
    classify_type,
    issuer_from_title,
    to_row,
)

__all__ = [
    "COLUMNS",
    "ISSUERS",
    "DeclarationFacts",
    "Issuer",
    "build_name",
    "classify_type",
    "issuer_from_title",
    "to_row",
]
