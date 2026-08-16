# Architecture

The decisions that are not obvious from reading the code, and why they were
made that way. Most of them were made because the obvious alternative fails
quietly rather than loudly, which in finance output is the dangerous direction.

---

## The shape

```
                    ┌──────────────────────────────┐
   web UI ────┐     │                              │
   CLI     ───┼────▶│   pdf2csv.core.pipeline.run  │
   notebook ──┘     │                              │
                    └──────────────┬───────────────┘
                                   │
      ┌──────────┬─────────────────┼───────────────┬──────────────┐
      ▼          ▼                 ▼               ▼              ▼
   router     digital           scanned        normalize      validate
  per page   text layer      raster+OCR      types, totals    the gate
                  └──── grid.py (shared geometry) ────┘
                                   │
                                   ▼
                        export.py  →  CSV + sidecar + xlsx
```

One public function. The interface, the command line and the notebook are
interchangeable callers, and none of them contains extraction logic. That is
what makes the front end replaceable without touching anything that matters.

---

## Why the stack is what it is

The client constraints — Windows, no admin, possibly no internet, commercial
delivery — eliminate most of the popular choices before any technical
comparison happens.

| Job | Chosen | Rejected | Reason |
|---|---|---|---|
| Text and tables | `pdfplumber` | camelot | camelot pulls in Ghostscript (AGPL) |
| Rasterising | `pypdfium2` | `pdf2image` + poppler | poppler is a system binary; pypdfium2 is a pure wheel |
| Rendering | `pypdfium2` | PyMuPDF | PyMuPDF is AGPL — unusable for commercial delivery |
| OCR | `rapidocr-onnxruntime` | PaddleOCR, Surya | ONNX models are 16 MB; torch stacks are multi-GB |
| Vision | `opencv-python-headless` | `opencv-python` | headless drops the GUI stack |
| UI | FastAPI + hand-built page | Gradio, Streamlit | see below |
| Packaging | embeddable Python | PyInstaller | PyInstaller fights native extensions and fails opaquely |

**On the UI.** The source guide specified Gradio, and for a tool whose job is
"upload a file, get a file", that is a reasonable default. It was not chosen
here because the validation report is the product. Gradio can render a JSON
blob; it cannot render a verdict banner, a per-check hint written for a
non-technical reader, and a data grid whose flagged cells line up with the rows
the report names. FastAPI plus about 900 lines of hand-written HTML, CSS and JS
gives full control, and is roughly 110 MB lighter in the bundle.

**On the OpenCV split.** `rapidocr-onnxruntime` declares a hard dependency on
`opencv-python` — the GUI build. Both packages own the same `cv2` module, so
installing normally overwrites the headless one and drags a Qt/GTK stack onto a
locked-down desktop. It is therefore installed with `--no-deps`, with its real
dependencies declared by hand in `requirements-ocr.txt`. The build script
verifies which `cv2` survived and repairs it if the wrong one won.

---

## The decisions that matter

### 1. Routing is per page, and the biggest performance lever there is

OCR costs 10–60 seconds a page; reading a text layer costs milliseconds. A
50-page document with 4 scanned pages should pay the OCR bill four times, not
fifty. Everything else in the performance budget is rounding error next to
getting this right.

Two refinements that are not obvious:

- A page with **an image and only a little text** is a scan carrying a bad
  prior OCR layer. It is re-read properly.
- A page with **a text layer and no image** is never sent to OCR, even if it
  contains no table. Prose is not a table, and OCR-ing a cover letter costs a
  minute and invents a one-column result. An earlier version got this wrong,
  and the "no table found" case became a fictional table.

### 2. Column boundaries are inferred document-wide, on both paths

This is the highest-impact correctness decision in the system.

pdfplumber decides columns one page at a time. A statement whose debit column
happens to be empty on page 1 comes back with five columns there and six on
page 2. The two no longer look like the same table, grouping splits them, and
the analyst silently receives half their statement.

So both the digital and the scanned path pool positioned text from every page
into one virtual coordinate space, infer the boundaries once, and apply them
everywhere. `grid.py` is shared by both because once you have positioned text,
the problem is identical whether it came from a text layer or a recogniser.

### 3. Borderless columns come from whitespace corridors, not clustering

The obvious approach is to cluster the left edges of text boxes. It fails badly
on finance tables, because amounts are right-aligned and labels are
left-aligned — clustering x-starts scatters a single amount column across
several phantom boundaries.

Instead, every text box is projected onto the x-axis and the vertical channels
that no row crosses are located. A corridor is a corridor regardless of how the
text on either side is aligned.

### 4. The table band is found before the columns are

A page is not only its table. A bank name, an address block, an account line and
a page footer are all runs of text lying across the same x-range the table
occupies, and letting them vote on column positions erases any corridor they
happen to cross. In testing, three letterhead lines were enough to merge `Date`
into `Libellé`.

Body rows are identified by their *rhythm*: a table is set on a constant
vertical pitch, which is what makes it look like a table, while titles and
footers sit at irregular distances from their neighbours. The tolerance is tight
(18%) because rows of one table vary by a few percent at most, and a run may
bridge one irregular row so that a wrapped description does not cut the table in
half.

### 5. Totals rows are lifted out, not deleted

A `TOTAL` row is not a transaction. Left in the dataframe it double-counts every
column sum. Simply deleted, the document's own arithmetic is thrown away.

Instead it is removed from the data and kept as the figure the extracted rows
must reconcile against. That is what makes validation able to say anything at
all: without it, there is nothing to check the extraction against except itself.

The match requires the label cell to be *essentially* the keyword — a
transaction described as "Total fees debited during the March billing period" is
a transaction, and pulling it out would silently delete a row.

### 6. Locale is decided once per document

`1.234` is 1234 in a French statement and 1.234 in an American one. No amount of
cleverness resolves that from the single cell, so every amount in the document
votes, weighted by how conclusive each pattern is, and the decision is applied
uniformly. Same for day-first versus month-first dates.

### 7. `None` beats a plausible wrong number

`parse_amount` returns `None` rather than guessing. An unparsed cell appears in
the validation report and gets looked at; a silently mangled one becomes a wrong
figure in a client's accounts.

This is why `"Page 1 of 12"` returns `None` rather than `112` — a naive
"strip everything that is not a digit" implementation gets that wrong, and it is
a real string that appears in real statements.

### 8. OCR digit repair is tightly gated

`O`→`0`, `l`→`1`, `S`→`5`, `B`→`8` is correct inside a number and catastrophic
inside a name. It runs only on cells in a column already established as numeric,
only in rows that came from OCR, and only where a digit sits within two
characters. `repair_ocr_digits("B2B")` returns `"828"` — which is exactly why the
gating exists, and there is a test asserting it to document the danger.

### 9. Failures annotate, they never block

An analyst who cannot get their data out will export it some other way and lose
the report entirely. So the CSV is always written, the failures are loud and
specific, and the report names the rows involved.

Warnings deliberately do not sink the overall verdict. If they did, one fuzzy
scanned digit would read as "failed" and the distinction between "check this
cell" and "your totals are wrong" would be lost.

### 10. A check that cannot run does not fail

A bank statement has no journal-style debit/credit balance; a document with no
stated totals has nothing to reconcile against. Checks that cannot find their
inputs are skipped and simply do not appear. A report full of failures meaning
"this document does not have a balance column" trains people to ignore reports.

---

## Concurrency and state

Jobs run on a two-worker thread pool and live in memory. This is one analyst on
one desktop, not a service — a database would be scaffolding for a requirement
that does not exist.

Two workers, not more: OCR is CPU-bound and saturates the cores it is given, so
four concurrent jobs make all four slow rather than any of them fast, and the
analyst is watching one of them.

Uploaded PDFs and their outputs are swept after 40 jobs. Client documents
accumulating indefinitely on someone else's machine is a slow-motion
data-retention problem.

---

## Caching

OCR output — recognised text, detected rules, page dimensions — is cached by the
source file's SHA-256. Caching *OCR results* rather than finished CSVs is
deliberate: during development the same scanned document is re-run dozens of
times while tuning a profile or a validation rule, and a finished-result cache
invalidates on every change while this one survives all of it.

Every cache failure mode — missing file, corrupt JSON, unwritable directory — is
a miss, never an error. A broken cache must degrade to slowness, not to a failed
extraction on a client desktop.

---

## Offline, enforced rather than assumed

Three things reach for the network if you let them, and all three are stopped:

| Would phone home | Stopped by |
|---|---|
| FastAPI's `/docs` (Swagger UI is CDN-hosted) | `docs_url=None`, asserted by a test |
| A stray remote font or script in the page | `Content-Security-Policy: default-src 'self'` |
| HuggingFace-style model loaders | `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` in the launcher |

A test asserts that no `http://` or `https://` reference exists anywhere in the
shipped HTML, CSS or JavaScript.

The OCR weights ship **inside the wheel**, so there is no model download on
first run at all — which removes the usual reason this class of tool fails on an
air-gapped machine.

**None of this substitutes for the real test:** a clean Windows machine, network
adapter disabled, end to end. A cached model or an installed package on the
build machine hides a first-run download perfectly.

---

## Python isolation in the bundle

The embeddable runtime must enable `import site` or nothing imports. That also
switches on the per-user site-packages directory, which means the *client's*
`%APPDATA%\Python\Python311\site-packages` lands on the application's import
path and can shadow the versions that shipped.

The launchers set `PYTHONNOUSERSITE=1`, `PYTHONPATH=` and `PYTHONHOME=`, and
`pdf2csv check` reports any import path originating outside the installation.
This was found by building the bundle and noticing pip resolving against
packages belonging to an unrelated project on the build machine — the symptom on
a client desktop would have been an import error reproducing on exactly one
machine.

---

## Where this still breaks

Stated so nobody has to rediscover it.

- **Merged cells spanning columns.** Forward-filling is available per profile
  and off by default, because filling wrongly fabricates data that reconciles
  perfectly and is wrong.
- **Two-line stacked headers on borderless pages.** One header row is taken
  above the detected band; the top line of a stacked header is lost. Taking two
  would pull the account-number line into the data, which is the more expensive
  mistake.
- **Columns whose whitespace gap is narrower than the minimum corridor** — about
  2.5 mm at 300 DPI.
- **Tables with genuinely irregular row pitch throughout**, where band detection
  finds no rhythm and falls back to using every row.

The documented escalation path for borderless scans that stay inaccurate is a
table-structure model (TableTransformer exported to ONNX), which stays inside
the no-torch constraint. Do not start there — line detection first, and only
escalate if fixtures prove it necessary.
