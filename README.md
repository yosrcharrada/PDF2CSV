# PDF2CSV

Turn finance PDFs into validated CSV files — offline, on a locked-down Windows
desktop, with no installation and no admin rights.

The analyst double-clicks one file, drops in a bank statement, and gets a
spreadsheet **plus a report saying whether the numbers reconcile**. That second
half is the point of the project.

---

## The two rules everything follows

**1. No logic outside `core/`.**
The web UI, the CLI and the notebook are thin callers of one function,
`pdf2csv.run()`. This is what makes "prototype here, deploy there" a
non-event rather than a rewrite.

**2. The CSV is not the deliverable. The CSV *plus its validation report* is.**
Finance output that has not been reconciled is worse than no output, because it
looks trustworthy. Every export writes a `.validation.json` sidecar next to it,
so a file found on a shared drive three weeks later still carries proof of
whether its totals ever added up.

---

## What it does

- Decides **per page** whether to read the text layer or run OCR, so a 50-page
  document with 4 scanned pages pays the OCR cost four times, not fifty.
- Handles ruled tables, borderless tables, and the very common case of vertical
  rules with no horizontal ones.
- Infers column boundaries **across the whole document**, so page 1 and page 9
  agree on how many columns there are.
- Parses money the way finance actually writes it: `(1,234.56)`, `1.234,56`,
  `1 234,56 TND`, `1,234.56 CR`, `1234.56-`.
- Normalises dates to ISO, deciding day-first or month-first once per document
  rather than guessing per cell.
- Reconciles the result against the document's own stated totals and running
  balance, and names the row when it does not agree.

## What it does not do

Stated plainly, because being explicit about this protects you far more than it
costs — see [`docs/SUPPORTED_FORMATS.md`](docs/SUPPORTED_FORMATS.md).

- It does not handle layouts it has never seen. A new bank template is a
  development task, not a bug.
- It does not catch semantic errors. A column that sums correctly but was
  mislabelled in the source passes every check.
- It does not rescue bad scans. Skew, low resolution and handwriting all
  degrade results sharply, and the report says so when confidence is low.

---

## For the analyst

You do not need this page. You need
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) — one page, no jargon.

## For whoever installs it

[`docs/INSTALL.md`](docs/INSTALL.md) covers both routes: handing over a
self-contained folder that needs nothing preinstalled, and setting up a
development checkout.

---

## Developer quick start

```bash
python -m venv .venv && .venv\Scripts\activate

pip install -e ".[dev]"

# Scanned-document support, in two steps — see below for why.
pip install -r requirements-ocr.txt
pip install --no-deps -r requirements-ocr-nodeps.txt

python -m pdf2csv check     # confirms cv2 reports 4.x, not 5.x
pytest
```

> **Why two steps for OCR.** `rapidocr-onnxruntime` declares a hard dependency
> on `opencv-python` — the GUI build. It and `opencv-python-headless` own the
> same `cv2` module, so a plain `pip install ".[ocr]"` installs both and
> whichever lands last wins, dragging a Qt stack onto a machine that is meant
> to be minimal. Installing it with `--no-deps` keeps the headless build.
> `pdf2csv check` reports which one you ended up with.

Run the interface:

```bash
python -m pdf2csv ui
```

Convert one file without the UI:

```bash
python -m pdf2csv convert statement.pdf -o statement.csv
```

Check an installation — the first thing to run when something is wrong:

```bash
python -m pdf2csv check
```

Build the portable Windows bundle for delivery:

```bash
powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -Zip
```

---

## Using it as a library

The whole public surface:

```python
from pdf2csv import run, export_result

result = run("statement.pdf")

print(result.report.summary())      # "All 7 checks passed."
print(result.dataframe.head())      # a pandas DataFrame

export_result(result, "statement.csv")   # writes CSV + sidecar + workbook
```

`run()` does not raise for a document it merely dislikes. A PDF with no tables,
or a scan on a machine without the OCR add-on, comes back as a result carrying a
failed check that says so in plain words. It raises only when the file cannot be
read at all.

---

## Project layout

```
src/pdf2csv/
├── core/
│   ├── pipeline.py    the only public entry point
│   ├── router.py      digital vs scanned, decided per page
│   ├── digital.py     text-layer extraction
│   ├── scanned.py     rasterise, deskew, OCR, rebuild
│   ├── grid.py        the geometry shared by both paths
│   ├── amounts.py     money and dates
│   ├── normalize.py   column typing, totals separation
│   ├── stitch.py      multi-page assembly, header de-duplication
│   ├── validate.py    the reconciliation checks
│   ├── export.py      CSV + sidecar + Excel workbook
│   └── cache.py       OCR results, keyed by file hash
├── profiles/          per-format knowledge, as YAML
├── server/            FastAPI app + the interface
└── cli.py             ui / convert / check / cache
```

---

## Documentation

| Document | For | Covers |
|---|---|---|
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | the analyst | One page. Start it, convert a file, read the verdict. |
| [`docs/INSTALL.md`](docs/INSTALL.md) | whoever deploys it | Building the portable bundle; development setup; every setting |
| [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) | everyone | Numbered requirements, and what is explicitly out of scope |
| [`docs/SUPPORTED_FORMATS.md`](docs/SUPPORTED_FORMATS.md) | the client | What works, what does not, how to add a format |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | maintainers | The decisions that are not obvious from the code |
| [`docs/PRE_DELIVERY.md`](docs/PRE_DELIVERY.md) | whoever hands it over | The checklist, including the two known blockers |
| [`docs/LICENSING.md`](docs/LICENSING.md) | legal / procurement | Why nothing copyleft ships, and how to re-audit |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | maintainers | Conventions, and things that look like bugs but are not |

There is also a Jupyter front end at
[`notebooks/template.ipynb`](notebooks/template.ipynb) — the same `run()`, for
when you need to see intermediate values rather than press a button.

---

## Licence

MIT — see [`LICENSE`](LICENSE). Every shipped dependency is MIT, BSD or
Apache-2.0; nothing copyleft enters the delivered set. The rationale and the
audit procedure are in [`docs/LICENSING.md`](docs/LICENSING.md).
