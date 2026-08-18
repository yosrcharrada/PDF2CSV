# Certificat de dépôt declarations → standard CSV

A different problem from the rest of this tool, and worth stating plainly.

`pdf2csv.core` answers *"what table is in this PDF?"*. These documents are not
that. Their structure is fixed and known in advance, and the CSV is not a
transcription of the table — it is a **derived row in a schema the document
never mentions**.

Only five facts come out of the PDF:

| Fact | Where | Feeds |
|---|---|---|
| Title token | `DECLARATION **CIL** 49-2026` | issuer, BIC, name |
| Taux | Taux column | rate, name |
| Quantité | Quantité column | totalNumberOfCertificates, nominal, quantity |
| Date de souscription | Date de souscription | auction/issue/start/entitlement dates, type |
| Date de remboursement | Date de remboursement | maturityDate, type, name |

Prix unitaire and Montant are read **for reconciliation only** and never reach
the output row. The whole FICHE DU SOUSCRIPTEUR block is unused while
`client = NO`.

That inversion is why this is a subpackage rather than a document profile. A
profile describes how to read a table; here there is almost no table-reading,
and instead a body of business rules that must be exact, versioned and
independently testable.

**The practical payoff:** accuracy only has to hold for a rate, an integer and
two dates. That is a far smaller promise than reconstructing every cell, and it
survives a 200 DPI scan comfortably.

---

## Status

| Piece | State |
|---|---|
| `declarations/mapping.py` — fields 2–22 | **Done.** Reproduces the reference row exactly |
| `declarations/facts.py` — OCR the five facts | **Done** for single-declaration documents |
| Reference case, end to end from the scan | **Verified.** All 22 fields, all 4 checks |
| ISIN allocation | **Done.** The workbook ships with the project and is found automatically |
| Multi-row *Billet de Trésorerie* documents | **Not supported** — see below |
| UI | **Done.** Dropping a declaration in the browser produces the row |

36 tests cover this subpackage. None of them use a client PDF. The ISIN
workbook is committed deliberately — see below — and is the only client file in
the repository.

---

## What the scans actually look like

Three properties of the real documents drove the design. None were in the spec.

**They are landscape content on a portrait page.** Every page arrives rotated a
quarter turn and `page.rotation` reads `0`, so nothing in the file says so. The
direction is not even consistent within one document — in the sample, page 1 is
rotated one way and page 2 the other.

Orientation is therefore resolved by **trying each rotation and keeping the one
that parses**. "Which way up is the text" and "where is the centre of mass" were
both implemented and both picked the wrong answer on at least one real page: the
recogniser silently corrects upside-down text, so the two candidates look nearly
identical. Parsing cannot be fooled the same way — a page read the wrong way
round does not produce a date under the *Date de souscription* heading.

**Headings and values do not arrive tidily.** All three of these are real:

- `Date de` and `souscription` come back as two boxes on two lines
- `Taux Prix unitaire` comes back as **one** box spanning two value columns
- the rate arrives glued to a date with no separator: `31/07/20268,00%`

Extraction is anchored on the printed headings, joins vertically adjacent boxes
to recover wrapped headings, splits a merged heading box proportionally between
the words in it, and strips date patterns before looking for a percentage. A
naive percentage match on that last string returns **268,00**.

**One sample document cannot be opened by pdfplumber at all** — it reports zero
pages while pdfium renders it perfectly. This path therefore opens PDFs with
pypdfium2, which costs nothing since these are scans with no text layer.

---

## Decisions taken, and the evidence

| Decision | Basis |
|---|---|
| BIC by **prefix rule**: CIL → `CILTTN00020` | The source SWIFT table has its code column sorted alphabetically while the name column keeps document order, so only BTK Leasing and Tunisie Leasing line up. Taken literally it gives CIL `BHLSTN00020`. Confirmed. |
| "Date actuel" = **date de souscription** | Confirmed. This is what makes the mapping a pure function — a processing-date reading would produce a different row on reprocessing. |
| `nominal` = **500 × quantité**, hardcoded | Confirmed. Not derived from prix unitaire, which is 500 000,000 and would give 5 000 000. |
| Multi-client grouping = same issuer + taux + both dates | The source phrasing compares souscription to remboursement, which cannot be the intent — they are never equal. |
| `deport` spelling kept as printed | If the downstream system string-matches, "correcting" it breaks every Coupon row while looking like an improvement. |

---

## The ISIN pool is distributed — and the ledger is not

The workbook ships with the project, so a clone allocates real codes with no
configuration. That is deliberate. The consequence is not obvious and matters:

**Consumed codes are recorded per machine.** The ledger (`isin_ledger.json`)
lives beside the logs and is never committed — it cannot be, since it changes
on every run. So two people working from their own clones will be handed the
**same ISINs**, and neither will know.

That is safe for one analyst on one desktop, which is the deployment this was
built for. It is not safe for two. Before a second person starts issuing codes,
one of these has to be true:

- one machine does all the allocation, or
- the ledger lives on a shared drive and everyone points at it, or
- each machine gets its own block in its own workbook.

The tool cannot detect the clash: from inside one clone, a code consumed
elsewhere looks unused. Exhaustion is loud; collision is silent.

---

## Open questions

These are unchanged from the spec and still block or risk work. Each has a
concrete consequence, so none is cosmetic.

| # | Question | Consequence if wrong / missing |
|---|---|---|
| 1 | **`code`** — the subscriber's RIB. It also determines `BIC`, and appears nowhere in the declaration PDF | Two columns cannot be produced at all |
| 2 | **`nominalValueAllotted`** — confirmed as 500 × quantity, but all four reference rows show 500 000 × quantity | Every row is out by a factor of 1000 |
| 3 | **`auctionDate`** — the subscription date on the CIL row, the fiche date on the BTK rows | Three date columns |
| 4 | **`amountToBePaid`** — zero on two BTK rows, Montant Net on the third | Silent, and looks ordinary either way |
| 5 | `startDate `, `guarantor `, `entitlement Date` carry stray spaces in `TCN CIL.csv` but not in the BTKL file | Header mismatch on import. The clean spelling is used |
| 6 | Title tokens for the **seven non-CIL issuers** | Only CIL is confirmed; the rest are inferred from code prefixes. A wrong token silently attributes a row to another company |
| 7 | `client` is `no` in one reference file and `No` in the other | String-matched downstream |
| 8 | BH Leasing issuer code ends **`00004`** where all others end `00003` | Affects every BHL row, and nothing else would reveal it |
| 9 | `certificat de **deport** sup 1an TF` — typo, or load-bearing? | Affects every Coupon row |

---

## Not supported: multi-row *Billet de Trésorerie* documents

The second sample (`FICHE SOUSCRIPTEUR`) is a **different document class**, not a
harder version of the first:

- no `DECLARATION` title — it is headed by the issuer's name
- **several instruments per page**, one row each, not one declaration per page
- extra columns with no mapping rule supplied: `NOMBRE DE JOURS`, `Intérêt Brut`,
  `Retenue à la source`, `Intérêt Net`, `Montant Net`
- libellés encode the instrument differently: `SERBTKL8.40%CD31072026`

It is correctly rejected rather than half-read, which is the right failure: a
partially understood financial document is worse than one that says it was not
understood.

Supporting it needs a decision on whether each table row becomes its own
standard row, and what the interest columns map to — if anything.
