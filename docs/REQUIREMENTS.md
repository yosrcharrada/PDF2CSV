# Requirements

What this system is required to do, what it is required *not* to do, and how
each requirement is verified. Numbered so they can be referred to in a review,
a change request or a bug report.

Every requirement below is either **implemented and tested**, or marked
explicitly as **out of scope**. Nothing here is aspirational.

---

## 1. Who this is for

**1.1** The primary user is a **finance analyst with no technical background**.
They have not asked for new software, will not read a manual, and cannot be
asked to open a terminal, edit a config file, or interpret a stack trace.

**1.2** The secondary user is **whoever supports them**. Every failure mode must
produce evidence that this person can act on without access to the analyst's
machine or their documents.

**1.3** The document owner is a **client whose financial data must not leave
their machine**. This constrains the architecture more than any functional
requirement does.

---

## 2. Deployment constraints

These are the constraints that eliminate most of the obvious technical choices.
They are not negotiable.

| # | Requirement | Consequence |
|---|---|---|
| **2.1** | Runs on **Windows** desktop | Wheels must be built and tested on Windows |
| **2.2** | **No admin rights** | No installer, no service, no registry, no PATH change |
| **2.3** | **No preinstalled software** — not even Python | Ship an embeddable runtime inside the folder |
| **2.4** | **No internet**, at install time or run time | All models and wheels bundled; verified with the adapter disabled |
| **2.5** | **Nothing written outside the delivered folder** | Falls back to `%LOCALAPPDATA%` only if the folder is read-only |
| **2.6** | **Commercial delivery** | No AGPL or GPL anywhere in the shipped set |
| **2.7** | Must survive **endpoint protection** | Binds loopback only; unsigned-executable allowance confirmed with the client early |

**2.8** The delivered folder must be **relocatable**. Copying it to a desktop, a
USB stick or a network share must not break it.

*Verified by:* `packaging/build_portable.ps1` runs the bundle's own
`pdf2csv check` as the final build step and fails the build if it does not pass.
The offline test on a clean machine is a manual pre-delivery gate — see
[`docs/PRE_DELIVERY.md`](PRE_DELIVERY.md).

---

## 3. Functional requirements

### 3.1 Reading documents

**3.1.1** Accept a single PDF, up to 200 MB (configurable), up to 500 pages.

**3.1.2** Classify **each page** independently as digital, scanned or empty.
Per-document classification is explicitly rejected: finance PDFs routinely
staple a typed statement to a scanned annex.

**3.1.3** Read digital pages via the text layer, at a cost of milliseconds.

**3.1.4** Read scanned pages via OCR at 300 DPI, at a cost of roughly 10–60
seconds per page.

**3.1.5** A page carrying **both** an image and a thin text layer — the
signature of a bad prior OCR pass — must be re-read through the scanned path.

**3.1.6** A page with a genuine text layer and no image must **never** be sent
to OCR, even when it contains no table. Prose is not a table, and OCR-ing a
cover letter costs a minute and produces a fictional one-column result.

### 3.2 Reconstructing tables

**3.2.1** Support ruled tables, borderless tables, and tables with vertical
rules only.

**3.2.2** Derive column boundaries from the **whole document**, not per page.

> Rationale: a statement whose debit column happens to be empty on page 1 yields
> five columns there and six on page 2. The two stop looking like one table and
> the document silently splits in half. This is the single highest-impact
> correctness requirement in the document-reading half of the system.

**3.2.3** Exclude letterheads, address blocks and page footers from column
inference and from the extracted rows.

**3.2.4** Detect repeated headers on continuation pages and drop them.

> Rationale: a header row left in the data does not raise anything —
> `parse_amount("Debit")` returns `None`, so the row lands as blanks and the
> column total quietly disagrees with the document by exactly the rows that were
> eaten.

**3.2.5** Concatenate pages **before** validating, never after.

### 3.3 Interpreting values

**3.3.1** Parse accounting negatives `(1,234.56)`, sign suffixes `1,234.56 DR` /
`CR`, trailing minus `1234.56-`, and unicode minus variants.

**3.3.2** Parse both separator conventions, deciding **once per document** from
the evidence in that document.

**3.3.3** Strip currency symbols and ISO codes.

**3.3.4** Return "not a number" rather than a plausible wrong number. `Page 1 of
12` must not become `112`.

**3.3.5** Preserve identifiers as text. `0041123` must not become `41123`; a
16-digit account number must not arrive in Excel as `1.23457E+15`.

**3.3.6** Normalise dates to ISO `YYYY-MM-DD`, deciding day-first vs month-first
once per document.

**3.3.7** Repair OCR digit confusion (`O`→`0`, `l`→`1`, `S`→`5`, `B`→`8`) **only**
in cells within a column already established as numeric, and **only** in rows
that came from OCR. Applying it to a text column corrupts names.

### 3.4 Validation — the gate

**3.4.1** Every export ships with a machine-readable validation report.

**3.4.2** Minimum check set:

| Check | Severity | Skipped when |
|---|---|---|
| A table was found | error | never |
| Every numeric cell could be read | error | never |
| Column totals match the document's stated totals | error | the document states none |
| Each row's balance follows from the previous row | error | no balance column |
| Total debits equal total credits | error | not a journal |
| No page inside the table was skipped | warning | never |
| No duplicate rows | warning | never |
| Scanned figures were read confidently | warning | no scanned pages |
| Expected columns are present | error | the profile declares none |

**3.4.3** A check that cannot find its inputs **does not run**, and says so by
its absence. A report full of failures meaning "this document has no balance
column" trains people to ignore the report.

**3.4.4** Failed checks **annotate** the export; they never block it.

> Rationale: an analyst who cannot get their data out will export it some other
> way and lose the report entirely. Loud and specific beats obstructive.

**3.4.5** A failed check must **name the rows involved**. "The totals do not
reconcile" sends someone to a 400-row CSV with no starting point.

**3.4.6** Warnings must not sink the overall verdict. Otherwise "check this
cell" and "your totals are wrong" look identical.

### 3.5 Output

**3.5.1** CSV encoded **UTF-8 with BOM**, so a double-click in Windows Excel
shows `Débit` rather than `DÃ©bit`.

**3.5.2** A `.validation.json` sidecar alongside every CSV, containing the
source file's SHA-256, page classification, every check, and every flagged cell.

**3.5.3** An `.xlsx` workbook with the data, the flagged cells highlighted with
the reason as a cell comment, the checks on their own sheet, and the document
metadata on a third.

**3.5.4** A copy of every output retained in the bundle's `output/` folder, so
results survive the browser's download behaviour.

---

## 4. Interface requirements

**4.1** One obvious action on first load: drop a PDF.

**4.2** Progress must be **live and specific** during long runs. A bar that sits
still for four minutes of OCR teaches the analyst that the tool has hung, and
the next thing they do is kill it halfway through a document.

**4.3** The validation verdict is the most prominent element on the results
screen — above the data, not below it.

**4.4** Failed checks are **expanded by default**. Making someone click to
discover what is wrong is the wrong default when what is wrong is the point.

**4.5** Every check states, in plain English, what happened and what to do about
it. No function names, no column indices, no stack frames.

**4.6** Flagged cells are highlighted in the preview grid, so a report naming
row 6 lands on a row that is already coloured.

**4.7** Numbers are right-aligned and set in tabular figures, so a misplaced
decimal is visible at a glance.

**4.8** Works at 1280 px and above; degrades to a single column below 940 px.
Keyboard accessible; respects `prefers-reduced-motion` and `prefers-color-scheme`.

**4.9** **No external asset of any kind** — no CDN, no webfont, no telemetry.
Enforced by a Content-Security-Policy header and asserted by a test.

---

## 5. Non-functional requirements

**5.1 Performance.** Digital pages: under 1 second for a typical statement.
Scanned pages: 10–60 seconds each on CPU. Total time scales with the *scanned*
page count, not document length.

**5.2 Caching.** OCR results are cached by file content hash, so re-running the
same document after a configuration change is near-instant.

**5.3 Memory.** Flat across long documents. Per-page caches are released as the
document is walked.

**5.4 Logging.** Rotating file log at INFO in `logs/`, seven files retained.
**Document contents are never logged** — filenames, page counts and check
results only. Client financial data does not belong in a log file.

**5.5 Privacy.** No network access at run time by any component. No analytics.

**5.6 Concurrency.** Two jobs at once. OCR is CPU-bound and saturates the cores
it is given; four concurrent jobs make all four slow rather than any of them
fast.

**5.7 Data retention.** Uploaded PDFs and their outputs are swept after 40 jobs.
Client documents accumulating indefinitely on someone else's machine is a
slow-motion data-retention problem.

**5.8 Failure behaviour.** No unhandled exception may reach the analyst. Every
error surfaces as a sentence they can act on or forward.

---

## 6. Explicitly out of scope

Stating these is what prevents them being assumed.

**6.1** Batch or folder processing. One file at a time, by design. Batches need
a job queue rather than a UI that hangs.

**6.2** Multi-user or networked deployment. Loopback only. This is a desktop
tool, not a service.

**6.3** Editing extracted data in the interface. Corrections happen in Excel,
against a CSV whose problems have been named.

**6.4** Automatic support for unseen layouts. A new bank template is a change
request with a fixture and an expected-output CSV attached.

**6.5** Handwriting recognition.

**6.6** Semantic validation. Checks confirm arithmetic, not meaning.

**6.7** PDF generation, editing or redaction.

**6.8** Password-protected PDFs. These fail with a clear message; the analyst
removes the password in their PDF reader first.

**6.9** macOS and Linux. The core is portable and the tests pass there, but the
packaging, the launchers and the delivery story are Windows-only.

---

## 7. Verification

| Requirement group | How it is verified |
|---|---|
| 3.1 routing | `tests/test_pipeline.py`, `tests/test_scanned.py` |
| 3.2 reconstruction | `tests/test_stitch.py`, `tests/test_scanned.py` (geometry) |
| 3.3 values | `tests/test_amounts.py`, `tests/test_normalize.py` |
| 3.4 validation | `tests/test_validate.py` — including a fixture that **must fail** |
| 3.5 output | `tests/test_pipeline.py::TestExport` |
| 4.9 offline UI | `tests/test_api.py::TestOfflineGuarantees` |
| 2.x packaging | `packaging/build_portable.ps1` self-test + manual clean-machine test |

The deliberately broken fixture (`statement_broken.pdf`) is the load-bearing
test: it has one transaction missing from the body while the totals row still
states the full figures. If it ever passes validation, the gate is broken and
every other test in the suite is meaningless.
