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
- Handles documents that hold **many unrelated tables**, not just one ledger:
  every table is extracted and validated separately, and the interface lets you
  pick between them rather than guessing on your behalf.

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

## Setup — step by step

Follow these in order. Every command below has been run on a clean machine from
a fresh clone.

### Step 0 — Check your Python version

**Supported: Python 3.10, 3.11 or 3.12.** Not 3.13 or newer — several compiled
dependencies have not been verified there yet, so the install deliberately
refuses rather than half-working.

Open a terminal and run:

```bash
python --version
```

| What it prints | What to do |
|---|---|
| `Python 3.10`, `3.11` or `3.12` | You're set — go to Step 1. |
| `Python 3.13` or newer | Install 3.12 **alongside** it (see below). You do not need to remove the newer one. |
| `Python 3.9` or older | Install 3.12 (see below). |
| `'python' is not recognized` | Python isn't installed, or isn't on PATH (see below). |

**Installing Python 3.12:** go to **<https://www.python.org/downloads/>**,
scroll to *"Looking for a specific release?"*, and pick the newest **3.12.x**.

On Windows, **tick "Add python.exe to PATH"** on the installer's first screen.
Almost every "it doesn't work" report traces back to that one box. Close and
reopen your terminal afterwards.

> If you install 3.12 next to a newer Python, `setup.bat` finds and uses 3.12
> automatically. Doing it manually, use `py -3.12` in place of `python` in
> Step 2a.

### Step 1 — Get the code

```bash
git clone https://github.com/yosrcharrada/PDF2CSV.git
```

```bash
cd PDF2CSV
```

No git? Download the ZIP from the GitHub page, extract it, and `cd` into the
extracted folder.

### Step 2 — Install

> **Windows shortcut:** double-click **`setup.bat`** in the project folder. It
> does Steps 2–4 for you — finds Python, creates the environment, installs
> everything in the right order, and checks the result. Then skip to Step 5.

Doing it manually, or on macOS / Linux:

**2a. Create a virtual environment.** This keeps the project's packages out of
your system Python and out of your other projects.

```bash
python -m venv .venv
```

**2b. Activate it.** Pick the line for your terminal:

| Terminal | Command |
|---|---|
| Windows **Command Prompt** (`cmd.exe`) | `.venv\Scripts\activate.bat` |
| Windows **PowerShell** | `.venv\Scripts\Activate.ps1` |
| macOS / Linux | `source .venv/bin/activate` |

Your prompt should now start with `(.venv)`. **If it doesn't, stop here** —
everything after this installs into the wrong place. See
[Troubleshooting](#troubleshooting).

**2c. Install the application:**

```bash
python -m pip install --upgrade pip setuptools wheel
```

```bash
pip install -e ".[dev]"
```

### Step 3 — Add scanned-document support

Skip this if you only ever convert PDFs that contain real text. Scanned pages
will then report a clear message instead of being read.

**This must be two commands. Do not combine them.**

```bash
pip install -r requirements-ocr.txt
```

```bash
pip install --no-deps -r requirements-ocr-nodeps.txt
```

> **Why two.** `rapidocr-onnxruntime` declares a hard dependency on
> `opencv-python` — the desktop-GUI build. It and `opencv-python-headless` both
> own the module named `cv2`, so a single `pip install ".[ocr]"` installs both
> and whichever lands last wins. That drags a Qt/GTK stack onto a machine meant
> to be minimal, and roughly triples the OpenCV footprint. `--no-deps` installs
> rapidocr without letting it pull the GUI build, and `requirements-ocr.txt`
> supplies what it genuinely needs.

### Step 4 — Verify

```bash
python -m pdf2csv check
```

You are looking for four things:

```
cv2                      4.14.0        <- 4.x, NOT 5.x
available        yes                   <- OCR is working
import paths     all inside this installation
Everything needed to run is present.
```

If `cv2` shows **5.x**, the GUI build won — see
[Troubleshooting](#troubleshooting).

### Step 5 — Run it

```bash
python -m pdf2csv ui
```

Your browser opens at **<http://127.0.0.1:8730>**. Drag a PDF onto the page.

**Leave the terminal window open** — that is the program running. Press
`Ctrl+C` there to stop it.

> On Windows you can instead double-click **`run.bat`**.

### Step 6 — Try it on the included samples

From the running page, drag in either file from `tests/fixtures/pdfs/`:

| File | What should happen |
|---|---|
| `statement_ruled_2page.pdf` | Green — *All checks passed*, 10 rows |
| `statement_broken.pdf` | Red — one credit missing, **row 6 named** |

The second is the one worth seeing. It is the same statement with a
transaction removed from the body while the totals row still claims the full
figures — exactly the failure this tool exists to catch.

---

## Certificat de dépôt declarations

A separate path for a different job. `convert` reads whatever table is in a PDF;
`declare` reads five known facts from a declaration and derives a fixed row from
them. See [`docs/DECLARATIONS.md`](docs/DECLARATIONS.md).

Read a declaration and print the row, without touching the ISIN pool:

```bash
python -m pdf2csv declare "DECLARATION CIL 49-2026.pdf" --dry-run
```

Allocate an ISIN and write the CSV:

```bash
python -m pdf2csv declare "DECLARATION CIL 49-2026.pdf" --isin-pool "block d ISIN.xlsx" -o out.csv
```

| Option | |
|---|---|
| `--isin-pool PATH` | The *block d ISIN* workbook. Omit and the ISIN column is left empty. |
| `--ledger PATH` | Allocation record. Defaults to `isin_ledger.json` beside the logs. |
| `--dry-run` | Show the row without consuming a code. |
| `--dpi N` | Default 200, which is enough for these documents. |

> **The ISIN pool ships with the project** — a clone allocates real codes
> with no setup. The ledger does not travel with it, so two people working
> from separate clones will be handed the same codes without either
> knowing. Safe for one analyst on one desktop; see
> [`docs/DECLARATIONS.md`](docs/DECLARATIONS.md) before it becomes two.
>
> **The ledger is the record of what has been issued, not the workbook.**
> Allocation is idempotent — re-running the same declaration returns the same
> ISIN rather than burning a second one, and two subscribers to the same
> issuance share a code. When a sheet runs out the export fails loudly rather
> than reusing or blanking a code.

Requires the OCR add-on (Step 3): these documents are scans.

## Other ways to run it

Convert one file without the browser. Exit code is `1` when the numbers do not
reconcile, so this is scriptable:

```bash
python -m pdf2csv convert statement.pdf -o statement.csv
```

Run the test suite (221 tests, about 10 seconds):

```bash
pytest
```


Build the portable Windows bundle for delivery to someone with no Python:

```bash
powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -Zip
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ERROR: Package 'pdf2csv' requires a different Python: 3.13.x not in '<3.13,>=3.10'` | Your Python is too new. Install 3.12 alongside it (Step 0) and create the environment with `py -3.12 -m venv .venv`. Nothing needs uninstalling. |
| `'python' is not recognized` | Python is not on PATH. Reinstall and tick *"Add python.exe to PATH"*, then reopen the terminal. |
| `error: externally-managed-environment` (Linux) | You skipped the virtual environment. Do Step 2a and 2b first. |
| `No module named venv` (Debian/Ubuntu) | `sudo apt install python3-venv`, then repeat Step 2a. |
| Prompt has no `(.venv)` after activating | You used the wrong activate command for your terminal — see the table in Step 2b. |
| PowerShell: *"running scripts is disabled"* | Run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, then activate again. No admin needed. |
| `cd "path"; command` fails in Command Prompt | `;` is PowerShell syntax. In `cmd.exe` use `&&`, and `cd /d` to switch drive. |
| `pdf2csv check` shows `cv2 5.x` | The GUI OpenCV won. Run `pip uninstall -y opencv-python opencv-python-headless` then repeat Step 3. |
| `pip check` says *"rapidocr-onnxruntime requires opencv-python"* | **Expected and correct.** We substituted the headless build deliberately. Not an error. |
| Install fails with `OSError: [Errno 2] No such file or directory` on a long `onnxruntime` path | Windows 260-character path limit. Move the project somewhere shorter, such as `C:\dev\PDF2CSV`. |
| Packages install but nothing imports | You installed outside the virtual environment. Check with `python -c "import sys; print(sys.prefix)"` — it must point at your `.venv`. |
| Port 8730 already in use | Nothing to do; it moves to the next free port and prints the URL it chose. |
| Scanned PDF says it cannot be read | Step 3 was skipped or failed. Re-run it, then `python -m pdf2csv check`. |

Still stuck? `python -m pdf2csv check` prints a full environment report designed
to be pasted into a bug report. It contains no document contents.

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
