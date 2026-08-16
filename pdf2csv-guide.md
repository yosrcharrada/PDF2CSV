# PDF → CSV for Finance Documents: Build & Delivery Guide

**Target:** Windows desktop, one analyst, no admin rights assumed, no internet assumed.
**Deliverable:** a folder the analyst double-clicks. Upload a PDF, get a validated CSV.

---

## 1. The two design rules

Everything below follows from these. Break them and the project gets hard later.

**Rule 1 — No logic in notebooks.**
Write a normal Python package. A notebook, a Gradio app, and a CLI are all thin callers of the same `run()` function. This is what makes "prototype here, deploy there" a non-event.

**Rule 2 — The CSV is not the deliverable. The CSV plus its validation report is.**
Finance output that hasn't been reconciled is worse than no output, because it looks trustworthy. Every export ships with a pass/fail report.

---

## 2. Stack choices (and why)

The client constraints — Windows, likely no admin, possibly no internet, commercial delivery — eliminate most of the popular options.

| Job | Use | Avoid | Reason |
|---|---|---|---|
| PDF text/tables | `pdfplumber` | camelot lattice | Camelot has pulled in Ghostscript (AGPL) |
| PDF → image | `pypdfium2` | `pdf2image` + poppler | Poppler is a system binary; pypdfium2 is a pure wheel |
| PDF rendering | `pypdfium2` | PyMuPDF | PyMuPDF is AGPL — bad for client delivery |
| OCR | `rapidocr-onnxruntime` | PaddleOCR, Surya | ONNX models are tens of MB; torch stacks are multi-GB |
| Line/table detection | `opencv-python-headless` | full `opencv-python` | Headless drops the GUI deps — smaller, fewer Windows issues |
| Dataframes | `pandas` | — | — |
| UI | `gradio` | Streamlit | Gradio bundles its own server, simpler to freeze |

**Net effect:** pip-only, permissively licensed, zero system dependencies. On a locked-down Windows desktop this is the difference between shipping and not shipping.

Verify licenses yourself before handoff — versions change. `pip-licenses` gives you the table in one command.

---

## 3. Project structure

```
pdf2csv/
├── core/
│   ├── __init__.py
│   ├── pipeline.py      # orchestration — the only public entry point
│   ├── router.py        # digital vs scanned, per page
│   ├── digital.py       # pdfplumber extraction
│   ├── scanned.py       # rasterize + OCR + table reconstruction
│   ├── normalize.py     # amounts, headers, multi-page stitching
│   ├── validate.py      # reconciliation checks
│   ├── notebook.py      # export() — validation-gated CSV writer
│   └── models.py        # dataclasses: ExtractedTable, ValidationReport
├── ui/
│   └── app.py           # Gradio
├── notebooks/
│   └── template.ipynb   # 3 cells, outputs cleared — copy per job
├── tests/
│   └── fixtures/        # real PDFs, one per format you support
├── models/              # pre-downloaded OCR .onnx weights
├── requirements.txt
├── pyproject.toml
└── build_portable.bat
```

---

## 4. The pipeline, stage by stage

### 4.1 Router — digital or scanned?

Decide **per page**, not per document. Finance PDFs routinely mix a typed statement with a scanned annex.

```python
# core/router.py
import pdfplumber

MIN_CHARS = 50

def classify_page(page) -> str:
    text = page.extract_text() or ""
    return "scanned" if len(text.strip()) < MIN_CHARS else "digital"

def classify_document(path: str) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [classify_page(p) for p in pdf.pages]
```

Edge case: some PDFs have a thin text layer over a scanned image (bad prior OCR). If text exists but extracted tables come back empty or ragged, fall through to the scanned path anyway.

### 4.2 Digital extraction

```python
# core/digital.py
import pdfplumber

LATTICE = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "intersection_tolerance": 5,
}

STREAM = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "text_tolerance": 2,
}

def extract_page_tables(page) -> list[list[list[str]]]:
    tables = page.extract_tables(LATTICE)
    if not tables or _is_ragged(tables):
        tables = page.extract_tables(STREAM)
    return tables or []

def _is_ragged(tables, tolerance=0.2) -> bool:
    """True if row lengths vary too much — sign the strategy picked wrong."""
    for t in tables:
        if not t:
            continue
        lengths = [len(r) for r in t]
        if max(lengths) == 0:
            return True
        if (max(lengths) - min(lengths)) / max(lengths) > tolerance:
            return True
    return False
```

`lines` works when the table has ruled borders. `text` works when columns are held together by whitespace alignment — common in bank statements. Try lattice first, fall back.

### 4.3 Scanned extraction

```python
# core/scanned.py
import pypdfium2 as pdfium
from rapidocr_onnxruntime import RapidOCR

_ocr = RapidOCR()  # loads once; point at models/ for offline

DPI = 300

def page_to_image(pdf_path: str, page_index: int):
    pdf = pdfium.PdfDocument(pdf_path)
    page = pdf[page_index]
    scale = DPI / 72
    return page.render(scale=scale, grayscale=True).to_pil()

def ocr_page(pdf_path: str, page_index: int):
    img = page_to_image(pdf_path, page_index)
    result, _ = _ocr(img)
    # result: [[box, text, confidence], ...]
    return [{"box": r[0], "text": r[1], "conf": r[2]} for r in (result or [])]
```

**300 DPI is the floor.** Below that, small statement fonts lose digits. Above 400 you pay a lot of time for little gain.

**OCR does not read tables.** RapidOCR returns positioned text boxes and nothing else. Table structure is something you reconstruct. This is the real work in the scanned path — budget for it accordingly.

**Preferred approach — detect ruling lines first, then OCR once.**

```python
import cv2
import numpy as np

def detect_grid(img_gray):
    bw = cv2.adaptiveThreshold(~img_gray, 255,
                               cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY, 15, -2)
    h, w = bw.shape
    horiz = cv2.dilate(cv2.erode(bw, np.ones((1, w // 40), np.uint8)),
                       np.ones((1, w // 40), np.uint8))
    vert  = cv2.dilate(cv2.erode(bw, np.ones((h // 40, 1), np.uint8)),
                       np.ones((h // 40, 1), np.uint8))
    intersections = cv2.bitwise_and(horiz, vert)
    return horiz, vert, intersections   # → real cell boundaries
```

Then: run OCR **once on the whole page**, and assign each text box to the cell its centre falls inside.

Two reasons this beats clustering. Anchoring to actual ruled lines is far more reliable than guessing boundaries from spacing. And one full-page OCR pass is much faster than cropping and OCR-ing hundreds of cells individually — that mistake alone can multiply your runtime by 10x.

**Fallback — geometric clustering, for borderless tables.**

```python
def boxes_to_grid(items, row_tol=10, col_tol=25):
    # 1. cluster by vertical centre → rows
    rows = _cluster([_ycentre(i["box"]) for i in items], row_tol)
    # 2. within the document, cluster x-starts → column boundaries
    cols = _cluster([_xstart(i["box"]) for i in items], col_tol)
    # 3. place each item into (row, col)
    grid = [["" for _ in cols] for _ in rows]
    for i in items:
        r = _nearest(rows, _ycentre(i["box"]))
        c = _nearest(cols, _xstart(i["box"]))
        grid[r][c] = (grid[r][c] + " " + i["text"]).strip()
    return grid
```

Derive column boundaries from the **whole document**, not per page. Per-page clustering drifts and your columns won't line up across pages.

**Cluster numeric columns on the right edge, not the left.** Financial tables right-align amounts and left-align labels. Clustering everything on x-start scatters your amount columns across phantom boundaries. This one detail causes a large share of borderless-table failures.

**Where reconstruction still breaks**, regardless of approach:

- Merged cells spanning multiple columns
- Text wrapping to a second line inside one cell — reads as an extra row
- Columns whose whitespace gap is narrower than `col_tol`

**Speed levers, in order of payoff:**

1. **Router accuracy** — the biggest lever by far. OCR-ing digital pages by mistake is pure waste. A 50-page document with 4 scanned pages should pay the OCR cost 4 times, not 50.
2. Grayscale and deskew before OCR
3. 300 DPI, not 400
4. Cache results by file hash so re-runs are instant

**Upgrade path if borderless scans stay inaccurate:** a table-structure model — TableTransformer exported to ONNX. Heavier, but stays inside the no-torch constraint. Do not start here; try line detection first and only escalate if your fixtures prove it necessary.

Carry OCR confidence through to the validation stage — low-confidence numeric cells are your highest-value review flags.

### 4.4 Normalization — where finance projects actually fail

**Amount parsing.** This one function will cause more bugs than the rest of the codebase combined.

```python
# core/normalize.py
import re

def parse_amount(s: str) -> float | None:
    if s is None:
        return None
    s = s.strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None

    neg = False
    # accounting negatives
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    # trailing debit/credit markers
    if s.upper().endswith(("DR", "DB")):
        neg, s = True, s[:-2]
    elif s.upper().endswith(("CR", "CT")):
        s = s[:-2]
    if s.startswith("-"):
        neg, s = True, s[1:]

    s = re.sub(r"[^\d.,]", "", s)          # strip currency symbols
    if not s:
        return None

    # decide which separator is decimal by position of the last one
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_comma > last_dot:
        s = s.replace(".", "").replace(",", ".")   # 1.234,56
    else:
        s = s.replace(",", "")                     # 1,234.56

    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v
```

**Also handle:**

- **OCR digit confusion** — `O→0`, `l/I→1`, `S→5`, `B→8`, only inside cells you've already decided are numeric. Never apply this to text columns or you'll corrupt names.
- **Repeated headers** — multi-page tables repeat the header on every page. Detect and drop before concatenating, or your totals are wrong.
- **Multi-page stitching** — concatenate *then* validate, never the reverse.
- **Merged cells** — pdfplumber emits `None` for spanned cells. Forward-fill down for merged row labels; decide explicitly per format.
- **Dates** — normalize to ISO `YYYY-MM-DD`. `03/04/2025` is ambiguous; infer the format from the document as a whole, not cell by cell.

### 4.5 Validation — the gate

```python
# core/validate.py
from dataclasses import dataclass, field

TOL = 0.01  # currency rounding

@dataclass
class ValidationReport:
    checks: list[dict] = field(default_factory=list)
    @property
    def passed(self) -> bool:
        return all(c["passed"] for c in self.checks)
    def add(self, name, passed, detail=""):
        self.checks.append({"check": name, "passed": passed, "detail": detail})

def check_column_total(df, col, stated_total, report):
    computed = df[col].dropna().sum()
    ok = abs(computed - stated_total) <= TOL
    report.add(f"total::{col}", ok,
               f"computed={computed:.2f} stated={stated_total:.2f}")

def check_balance_continuity(df, report,
                             opening="opening", debit="debit",
                             credit="credit", closing="closing"):
    expected = df[opening].iloc[0] + df[credit].sum() - df[debit].sum()
    actual = df[closing].iloc[-1]
    report.add("balance_continuity", abs(expected - actual) <= TOL,
               f"expected={expected:.2f} actual={actual:.2f}")
```

**Minimum check set for finance:**

1. Column sums reconcile against totals stated in the document
2. Debits equal credits (statements, journals)
3. Opening balance + movements = closing balance
4. Row count matches what page-level detection expected
5. No unparseable values in numeric columns
6. No low-confidence OCR cells in numeric columns

**Failures do not block export — they flag it.** The analyst gets the CSV plus a clear "these three checks failed." Silent success is the dangerous outcome, not failure.

### 4.6 Orchestration

```python
# core/pipeline.py
import pandas as pd
from . import router, digital, scanned, normalize, validate

def run(pdf_path: str) -> tuple[pd.DataFrame, dict]:
    kinds = router.classify_document(pdf_path)
    raw_tables = []

    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if kinds[i] == "digital":
                raw_tables += digital.extract_page_tables(page)
            else:
                items = scanned.ocr_page(pdf_path, i)
                raw_tables.append(scanned.boxes_to_grid(items))

    df = normalize.to_dataframe(raw_tables)
    report = validate.run_all(df)
    return df, report
```

One public function. The notebook calls it, the UI calls it, the tests call it.

---

## 5. The interface

Because all logic lives in `core/`, the front end is interchangeable. Two options below — Gradio (§5.1) and Jupyter (§5.2). Both call the same `run()`. You can ship either, or both from the same folder.

### 5.1 Gradio app

```python
# ui/app.py
import gradio as gr
from core.pipeline import run

def process(pdf_file):
    df, report = run(pdf_file.name)
    out = "output.csv"
    df.to_csv(out, index=False)
    status = "✅ All checks passed" if report["passed"] else "⚠️ Review required"
    return out, status, report

demo = gr.Interface(
    fn=process,
    inputs=gr.File(file_types=[".pdf"], label="PDF"),
    outputs=[
        gr.File(label="CSV"),
        gr.Textbox(label="Status"),
        gr.JSON(label="Validation report"),
    ],
    title="PDF → CSV",
    allow_flagging="never",
)

if __name__ == "__main__":
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        analytics_enabled=False,   # do not phone home on client financials
        inbrowser=True,
    )
```

Bind to `127.0.0.1`, not `0.0.0.0`, on a client desktop. No reason to expose it to their network.

### 5.2 Jupyter notebook path

Viable, and worth having — it's the better choice when the analyst wants to inspect intermediate output, tweak parsing rules for a new bank format, or debug a document that failed validation. Gradio gives you a button; Jupyter gives you a workbench.

**The risk to design around:** cells can be run out of order, edited, or skipped — including the validation cell. For finance output that's a real hazard, since a partially-run notebook still produces a CSV that looks finished. Two guardrails remove it.

**Guardrail 1 — the notebook holds no logic.** Same rule as everywhere else. Three cells, nothing to break:

```python
# Cell 1 — setup
import sys; sys.path.insert(0, "..")
from core.pipeline import run
from core.notebook import export
```

```python
# Cell 2 — pick a file
from ipyfilechooser import FileChooser
fc = FileChooser(".", filter_pattern="*.pdf")
display(fc)
```

```python
# Cell 3 — run and export
df, report = run(fc.selected)
export(df, report, "output.csv")   # export enforces the gate; see below
display(df.head(20))
```

**Guardrail 2 — validation lives inside `export()`, not in a cell.** It cannot be skipped, because there is no way to write the CSV without going through it:

```python
# core/notebook.py
def export(df, report, path):
    if not report["passed"]:
        failed = [c["check"] for c in report["checks"] if not c["passed"]]
        print(f"⚠️  {len(failed)} check(s) failed: {', '.join(failed)}")
        print("Writing anyway — review before use.")
    df.to_csv(path, index=False)
    _write_sidecar(report, path)   # output.validation.json next to the CSV
    return path
```

The sidecar JSON matters: it means a CSV found on disk three weeks later still carries proof of whether it reconciled.

**Packaging notes for the notebook route**

- Add `jupyterlab` and `ipyfilechooser` to `requirements.txt`, and download their wheels with the rest
- Notebook launcher, alongside `Run.bat`:

```bat
@echo off
cd /d "%~dp0"
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
python\python.exe -m jupyterlab --notebook-dir=app\notebooks
pause
```

- JupyterLab is fully offline once installed — it serves its own assets from `localhost`. No CDN dependency.
- Commit a `notebooks/template.ipynb` with outputs cleared. Have the analyst copy it per job rather than editing the original, so a broken notebook is never the only copy.

**Which to ship:** Gradio if the analyst just needs CSVs out. Jupyter if they'll be handling unfamiliar formats and need to see what went wrong. Shipping both costs almost nothing — same `core/`, two launchers.

---

## 6. Packaging for Windows

Don't use PyInstaller — it fights with ML dependencies and produces opaque failures. Use the **embeddable Python** distribution.

```
PDF2CSV\
├── python\        # python-3.11-embed-amd64, unzipped
├── app\           # core/ and ui/
├── models\        # RapidOCR .onnx weights, pre-downloaded
├── wheels\        # offline install cache
├── logs\
└── Run.bat
```

`Run.bat`:

```bat
@echo off
cd /d "%~dp0"
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
set GRADIO_ANALYTICS_ENABLED=False
python\python.exe app\ui\app.py
pause
```

Analyst double-clicks. Browser opens. No install, no admin, no terminal.

**Build the wheel cache on a Windows machine** — wheels are platform-specific, and a set downloaded on Linux will not install on the target.

```bat
pip download -r requirements.txt -d wheels
pip install --no-index --find-links wheels -r requirements.txt
```

Keep `pause` in the batch file. When it crashes on the client's machine, that window holds the traceback.

---

## 7. Offline readiness

Once installed, nothing here needs internet. But three things will silently reach for the network if you don't stop them:

| Needs network | Fix |
|---|---|
| Model weights on first run | Pre-download into `models/`, point the OCR constructor at local paths |
| HuggingFace `from_pretrained` pings the hub even when cached | `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` |
| Gradio telemetry | `analytics_enabled=False` + env var |

**The only real test:** on a clean Windows machine, disable the network adapter and run end to end. A cached model on your dev machine hides this failure perfectly — you will not catch it any other way.

---

## 8. Pre-delivery checklist

**Technical**

- [ ] Runs end to end with the network adapter disabled, on a machine that never had the project
- [ ] Runs without admin rights
- [ ] Fixture PDF per supported format, with an expected-output CSV committed
- [ ] Amount parser has unit tests covering: parentheses negatives, CR/DR suffixes, both locale separators, currency symbols, blank cells
- [ ] Rotating file log in `logs/`, at INFO
- [ ] `pip-licenses` output reviewed — no AGPL/GPL in the shipped set

**With the client, early**

- [ ] Endpoint protection allows an unsigned executable binding a local port — *this is the single most common late blocker*
- [ ] Agreed location for output CSVs
- [ ] Confirmed that input PDFs may sit on local disk during processing
- [ ] Named owner for post-handoff issues: new statement formats, changed bank templates

**Documentation**

- [ ] One-page runbook: how to start it, where output goes, what the validation report means, who to contact
- [ ] Written list of supported document formats — and an explicit statement of what is out of scope

---

## 9. Known limits — state these up front

Being explicit about these protects you far more than it costs you.

- **OCR is slow.** Roughly 10–60 seconds per *scanned* page on CPU. Digital pages cost milliseconds, so total time scales with the scanned page count, not document length. Fine for single files. For batches, add a job queue rather than letting the UI hang.
- **Unseen layouts will fail.** The system handles formats you've built fixtures for. A new bank template is a development task, not a bug.
- **Poor scans degrade badly.** Skew, low resolution, and handwriting all reduce accuracy sharply. Set the expectation that scan quality drives output quality.
- **Validation catches arithmetic errors, not semantic ones.** A correctly-summing column with a mislabelled header still passes every check.

---

## 10. Build order

1. Package skeleton + `run()` stub + one fixture PDF
2. Digital path only, one document format, end to end to CSV
3. Amount parser with its full unit test suite
4. Validation stage — get one real reconciliation passing
5. Front end — Gradio, notebook, or both (§5)
6. Scanned path
7. Multi-page stitching and header deduplication
8. Portable Windows bundle
9. Offline test on a clean machine
10. Runbook and handoff

Stages 1–4 are the project. Everything after is packaging.

A notebook is useful as your own development front end from stage 2 onward — as long as it only calls `run()` and `export()`, it costs nothing and you can ship the same file later.
