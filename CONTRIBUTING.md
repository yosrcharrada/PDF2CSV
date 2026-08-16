# Working on this codebase

Context for anyone — human or model — picking this up.

---

## The two rules

**1. No extraction logic outside `src/pdf2csv/core/`.**
The web UI, the CLI and the notebook are thin callers of `pdf2csv.run()`. If you
find yourself parsing a cell in `server/` or `cli.py`, stop: it belongs in
`core/` with a test.

**2. The CSV is not the deliverable. The CSV plus its validation report is.**
Never add a path that writes a CSV without also writing its report. Unreconciled
finance output is worse than none, because it looks trustworthy.

---

## The rule behind most of the code

**Returning "I don't know" beats returning a plausible wrong answer.**

`parse_amount("Page 1 of 12")` returns `None`, not `112`. An unparsed cell shows
up in the validation report and gets looked at. A silently mangled one becomes a
wrong figure in someone's accounts.

Apply this whenever you are tempted to add a fallback. Ask what happens if the
fallback is wrong. If the answer is "a number that looks fine and isn't", do not
add it.

---

## Before you change extraction behaviour

Run the fixtures. All four exist to catch a specific class of mistake:

| Fixture | Catches |
|---|---|
| `statement_ruled_2page.pdf` | Repeated headers eating page 2; identifiers losing leading zeros |
| `statement_borderless_fr.pdf` | Letterhead merging columns; European decimals; footers as data |
| `statement_broken.pdf` | **The gate itself.** If this ever passes, every other test is meaningless |
| `statement_scanned.pdf` | The whole OCR path, end to end, against a JPEG with no text layer |
| `letter_no_table.pdf` | Prose being OCR'd into a fictional table |

`statement_broken.pdf` is load-bearing. It is the ruled statement with one
transaction removed from the body while the totals row still states the full
figures. It **must** fail validation, and the failure must name row 6.

---

## Things that look like bugs and are not

- **`repair_ocr_digits("B2B") == "828"`.** Correct, and there is a test asserting
  it. The function is only ever called on cells in a column already established
  as numeric, in rows that came from OCR. The gating is the safety, not the
  function.

- **Checks that do not appear in the report.** A check with no inputs is skipped
  deliberately. A bank statement has no journal-style debit/credit balance; a
  document with no stated totals has nothing to reconcile against. Failing them
  instead would fill the report with noise and train people to ignore it.

- **Warnings do not fail the overall verdict.** Only `Severity.ERROR` does. If
  warnings sank the report, one fuzzy scanned digit would read the same as
  "your totals are wrong".

- **`min-height` rather than `height` on `body` in `app.css`.** A flex column
  with `height: 100%` clamps the page to the viewport and the verdict banner
  becomes unreachable. There is a comment; do not "tidy" it.

- **`..\app` in the bundle's `._pth`.** Entries resolve relative to the folder
  holding `python.exe`, not the working directory.

---

## Adding support for a new document format

It is usually configuration, not code:

1. Add a fixture — either the real PDF, or the layout reproduced in
   `tests/fixtures/make_fixtures.py`.
2. Write the expected output **by reading the document**, not by capturing what
   the code currently produces.
3. Run it. The generic path handles most layouts.
4. If it needs help, write a YAML profile in `src/pdf2csv/profiles/`.

Profiles **describe**, they do not compute. When a format differs structurally —
amounts split across two physical columns, three-level nested headers — that is
development work. Growing the profile schema to cover it is how configuration
systems turn into bad programming languages.

---

## Conventions

- **Comments explain why, never what.** The code says what it does. Comments
  carry the reasoning that would otherwise be lost — especially the alternative
  that was tried and failed.
- **User-facing strings are read by a finance analyst**, not an engineer. No
  function names, no column indices, no `(s)` — use `pdf2csv.wording`.
- **Never log document contents.** Filenames, page counts and check results are
  fine. Cell values are client financial data.
- **British spelling** in prose, US spelling where an API demands it
  (`normalize_table`, `color`).
- `ruff check src tests` must be clean.

---

## Environment

Tests need the fixtures generated once:

```bash
python tests/fixtures/make_fixtures.py
pytest
pytest -m "not ocr"     # skip the slow OCR round trip while iterating
```

The OCR extra installs in two steps — `rapidocr-onnxruntime` hard-requires the
GUI build of OpenCV and will overwrite the headless one. See
[`docs/INSTALL.md`](docs/INSTALL.md) §2.1.

---

## What "done" means here

The target user is a finance analyst on a locked-down Windows desktop with no
Python, no admin rights and possibly no internet. A change is not finished
because the tests pass. It is finished when it still works from the portable
bundle:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1
```

The build runs the bundle's own `pdf2csv check` and fails if it does not pass.
That catches a broken `._pth`, a missing DLL and a half-installed dependency —
every one of which otherwise surfaces first on the client's machine.

Full list: [`docs/PRE_DELIVERY.md`](docs/PRE_DELIVERY.md).
