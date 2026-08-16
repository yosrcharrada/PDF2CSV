# Supported document formats

Being explicit about this protects you far more than it costs. The system
handles the formats it has been built and tested against. Anything else may
work, and may work *almost* correctly, which is the outcome to be most careful
about.

Read the two lists below as a contract, not as marketing.

---

## Supported and tested

Each of these has a committed fixture and an assertion on its expected output.

### Ruled tables — full grid

Horizontal and vertical rules drawn around every cell.

- Header repeated on continuation pages, detected and dropped
- A stated `TOTAL` row, lifted out and reconciled against
- Identifier columns with leading zeros, preserved as text
- Anglo separators (`1,234.56`), `dd/mm/yyyy` dates

*Fixture:* `statement_ruled_2page.pdf`

### Borderless tables — no rules at all

Columns held together by whitespace alignment. The common case for bank
statements.

- Column boundaries inferred from whitespace corridors, pooled across all pages
- Letterhead and page footers excluded from the table
- Continental separators (`1.234,56`), inferred from the document
- Accented headers (`Libellé`, `Débit`) surviving into the CSV

*Fixture:* `statement_borderless_fr.pdf`

### Vertical rules only

Columns boxed, rows separated only by leading. Very common, and handled by a
dedicated strategy rather than falling back to either pure approach.

### Scanned documents

Pages carrying an image and no text layer.

- Rasterised at 300 DPI, deskewed, recognised in a single full-page pass
- Ruling lines used where drawn, whitespace corridors where not
- OCR confidence carried through to the validation report
- Digit confusion repaired inside numeric columns only

*Fixture:* `statement_scanned.pdf` — the ruled statement flattened to JPEG with
no text layer, asserted to recover the closing balance exactly.

### Documents holding several unrelated tables

An annual report, a rate card or a policy pack is not one table spanning pages;
it is a dozen tables about different things, several of which happen to share a
column count.

- Every table is extracted, and **every table is validated separately** — the
  report you read belongs to the table you are looking at.
- The interface lists them along the top with their size and page numbers, so
  you can pick the one you want.
- Each downloads as its own CSV. The Excel workbook contains all of them, one
  per sheet.

Two failure modes are specifically guarded against, because both were observed
on real documents:

- Tables are **not** merged just because they are the same width. A single
  ruled grid arrives as one table, so two tables on one page really are two
  tables, and a continuation onto the next page has to look like one — the
  first ending near the page bottom and the second resuming at the top, or the
  column titles repeating.
- No table is silently dropped in favour of the biggest.

*Fixture:* `multi_table.pdf` — three tables on one page, two of them the same
width, asserted to stay separate and all to be returned.

### Mixed documents

Digital and scanned pages in the same file. Classification is per page, so a
typed statement with a scanned annex costs OCR only on the annex.

### Documents with no table

Cover letters, terms and conditions. These produce a clean "no table found"
result — not a crash, and not a fictional one-column table built out of prose.

*Fixture:* `letter_no_table.pdf`

---

## Not supported

### Out of scope by design

| | |
|---|---|
| **Password-protected PDFs** | Remove the password in a PDF reader first. |
| **Handwriting** | Printed text only. |
| **Multi-column page layouts** | Newspaper-style flowing text is not a table. |
| **Charts and images as data** | Only tabular text is extracted. |
| **Nested or multi-level headers** | Two-row stacked headers work on ruled tables; three levels do not. |
| **Cells spanning multiple columns** | Merged cells are extracted but their span is not reconstructed. |
| **Rotated or landscape-in-portrait pages** | Deskew corrects tilt up to 15°, not 90° rotation. |
| **Batch or folder processing** | One file at a time. Batches need a job queue. |

### Degrades rather than fails

These produce output *and* a warning. Treat the warning as the answer.

| Situation | What happens |
|---|---|
| Poor scan quality | Lower OCR confidence; affected cells flagged amber |
| Skewed scan | Corrected up to 15°; beyond that, rows stop grouping |
| Below 300 DPI | Small digits lose strokes; `8` starts reading as `3` |
| Wrapped cell text | Kept in one cell, but may break band detection |
| Genuinely irregular row spacing | Letterhead may be included as data rows |

---

## Adding a new format

A new bank template is a **change request**, not a bug. The work is usually
small, and it is bounded:

1. **Get a real document.** A redacted one is fine; a retyped one is not, because
   the layout is the thing being reproduced.
2. **Add a fixture.** Either commit the real PDF (if it can be shared) or
   reproduce the layout in `tests/fixtures/make_fixtures.py`.
3. **Write the expected output first**, by reading the document. A golden file
   captured from the current code only proves the code still does what it did
   last time.
4. **Run it.** Often nothing further is needed — the generic path handles most
   layouts.
5. **If it needs help, write a profile** in `src/pdf2csv/profiles/`. See
   `example_bank_statement.yaml` for every available key.

A profile can describe:

- which decimal separator and date convention the format uses
- which columns are identifiers that must stay text
- what the totals row is called
- which columns play the opening / debit / credit / closing roles
- which columns must be present for the extraction to be considered correct

A profile **cannot** compute. When a format differs structurally rather than
descriptively — amounts split across two physical columns, a three-level nested
header — that is genuine development work, and growing the profile schema to
cover it is how configuration systems turn into bad programming languages.

---

## What the checks do and do not prove

**They prove** the extracted rows are arithmetically consistent with the
document: column totals match the stated totals, the running balance follows,
debits equal credits where that applies, no page inside the table was skipped.

**They do not prove** the data means what you think it means. A column that sums
correctly but was mislabelled in the source PDF passes every check, and always
will. Validation catches arithmetic errors, not semantic ones.
