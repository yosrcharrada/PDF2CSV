# Certificat de dépôt declarations → standard CSV

A different problem from the rest of this tool, and worth stating plainly.

`pdf2csv.core` answers *"what table is in this PDF?"*. These documents are not
that. Their structure is fixed and known in advance, and the CSV is not a
transcription of the table — it is a **derived row in a schema the document
never mentions**.

From a declaration, only five facts come out of the PDF. A fiche adds the
subscriber's identity and repeats the whole set once per row:

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
| `declarations/mapping.py` — fields 2–22 | **Done.** Reproduces both reference files |
| `declarations/facts.py` — one declaration a page | **Done** |
| `declarations/fiche.py` — many subscribers, columns across pages | **Done** |
| CIL declaration, end to end from the scan | **Verified.** Every field |
| BTK Leasing fiche, end to end from the scan | **Verified.** Every field but the three needing an account number |
| Subscriber columns 23–36 | **Filled** where the document names a subscriber |
| ISIN allocation and grouping | **Done.** Co-subscribers to one issuance share one code |
| UI | **Done.** Both document kinds go in the same box |

55 tests cover this subpackage. None of them use a client PDF: the fiche
reader's grid work is tested against a table the test itself draws. The ISIN
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
| `nominalValueAllotted` = **the montant printed** | Confirmed against all four reference rows. An earlier reading of 500 × quantité was out by a factor of a thousand on every row; the face value is 500 000, which is also the prix unitaire the documents print. |
| `numberOfCertificates` = **montant ÷ prix unitaire** | The only rule all four reference rows agree with. The fiche prints a quantité of 5 against a montant of 3 500 000 at 500 000 each, and its reference row says seven — the document contradicts itself, and the finance team followed the montant. |
| `auctionDate`/`issueDate`/`startDate` = **the date the document is dated** | Settled by the fiche: two of its rows are subscribed on 31/07 and one on 03/08, yet all three carry 03/08, the date the fiche was drawn. On the CIL declaration the two coincide, which is why a souscription reading looked correct there. |
| `entitlementDate` = **the date de souscription**, per row | Stays with the subscription while the three above move to the document date. Both reference files agree. |
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
| 1 | **`code`** — the subscriber's securities account. Printed in neither document | Three columns. The only one that blocks a complete row — see below |
| 2 | `startDate `, `guarantor `, `entitlement Date` carry stray spaces in `TCN CIL.csv` but not in the BTKL file | Header mismatch on import. The clean spelling is used |
| 3 | Title tokens for the **six unconfirmed issuers** | CIL and BTK Leasing are confirmed against real documents; the rest are inferred from code prefixes. A wrong token silently attributes a row to another company |
| 4 | `client` is `no` in one reference file and `No` in the other | String-matched downstream. `No` is written, matching the BTK Leasing file |
| 5 | Should columns 23–36 be **filled or blank** when the document names a subscriber? | They are filled, as asked. The finance team's own reference file for that document leaves them empty |
| 6 | BH Leasing issuer code ends **`00004`** where all others end `00003` | Affects every BHL row, and nothing else would reveal it |
| 7 | `certificat de **deport** sup 1an TF` — typo, or load-bearing? | Affects every Coupon row |

### Why `code` cannot be derived, and what it drags with it

It is the subscriber's securities account, and it decides two further columns:

| Row | `code` | `BIC` | `amountToBePaid` |
|---|---|---|---|
| CIL | `TN31`**`CILT`**`0201021LFIN12364001` | `CILTTN00020` | 0 |
| BTKL 1 | `TN50`**`AILE`**`0201021P00142218001` | `AILETN00020` | 0 |
| BTKL 2 | `TN80`**`AILE`**`0201021P00142217001` | `AILETN00020` | 0 |
| BTKL 3 | `TN62`**`UBCI`**`0201004LFIN09678001` | `UBCITNTT020` | 4 924 922,296 |

The four letters after the check digits are the custodian, `BIC` follows from
them, and the amount is zero exactly where the subscriber holds with the
issuing company itself. All three columns are therefore one fact — and that
fact is printed in neither the declaration nor the fiche.

What is written is the issuer's own BIC, an empty `code` and a zero amount:
right for three of the four reference rows, wrong for a subscriber banking
elsewhere, and reported as a check on every run rather than assumed.

---

## The fiche du souscripteur

A `FICHE SOUSCRIPTEUR` is a different document class from a declaration, not a
harder version of one, and three of its properties drove the reader.

**The table is split across pages by column.** Page one holds the subscriber's
identity, page two the instrument, and a row is the two halves at the same
position joined together. Nothing in either half says which row of the other it
belongs to — only the ordering does. That is the opposite of the continuation
in `core/stitch.py`, where a page adds rows to a fixed set of columns.

Because only ordering relates them, halves of different heights are refused
rather than paired up: a subscriber against the wrong instrument produces a row
that looks entirely ordinary and is wrong about who bought what.

**The pages are tilted about three degrees.** Invisible to a reader and fatal to
row grouping — a row's text drifts roughly a hundred pixels across the page
while the rows themselves are only eighty apart, so grouping by height alone
interleaves them. Every page is deskewed before recognition.

**Columns come from the printed ruling lines, not the headings.** A rule is a
single unambiguous x; a heading wraps onto two lines, merges with its
neighbour, or sits anywhere within its cell. On the sample the rules give the
twelve instrument columns and the seven identity ones exactly — including the
blank spacer column that also appears in the finance team's own spreadsheet.

Two smaller problems needed answers:

- The recogniser **runs neighbouring cells together**: `TUNISIENNE` and
  `CARTE D'IDENTITE` arrive as one box. Cutting the string proportionally lands
  mid-word — `TUNISIENNECA` — so the cell is re-read from its own pixels,
  cut at the ruling line, where no such mistake is possible.
- It **closes up the spaces in printed capitals**, so a name arrives as
  `SELMAELLOUMIREKIK`. At twice the size it reads them as written, which is why
  the identity columns are re-read enlarged. This is the deliberate exception to
  the warning in `core/ocr.py` against per-cell recognition: that warning is
  about losing context across a whole page of cells, and here the enlargement
  buys back more than the lost context costs.

Headings are matched approximately as well as exactly. Recognition of a heading
is not reliable enough to demand a literal match — real failures include a
clipped ending and an `l` read for an `i` — and a lost column is not a visible
failure but a row carrying a plausible wrong value. The threshold is set so that
`remboursement` can never answer for `souscription`, the one confusion that
would matter.

**The interest columns are read and unused.** `NOMBRE DE JOURS`, `Intérêt Brut`,
`Retenue à la source` and `Intérêt Net` have no column in the standard layout,
in the same way prix unitaire and montant have none on a declaration. They are
read for reconciliation, not exported.
