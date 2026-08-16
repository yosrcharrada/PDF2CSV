# Installing

Two audiences, two completely different procedures. Pick the right one.

- **[Getting it onto an analyst's PC](#1-getting-it-onto-an-analysts-pc)** —
  the person receiving it needs nothing installed and is not technical.
- **[Setting up a development checkout](#2-development-checkout)** — for
  whoever maintains it.

---

## 1. Getting it onto an analyst's PC

The analyst never installs anything. You build a self-contained folder once, on
a Windows machine with internet, and hand it over. Their machine needs no
Python, no admin rights, no internet and no configuration.

### 1.1 What you build

```
PDF2CSV\
├── Start PDF2CSV.bat        ← the only file they ever run
├── Check installation.bat   ← diagnostics, for when it will not start
├── READ ME FIRST.txt
├── Runbook.md
├── python\                  ← embeddable Python 3.11, ~15 MB
├── app\                     ← the pdf2csv package
├── output\                  ← finished CSVs land here
├── logs\
├── installed-packages.txt   ← exactly what shipped
├── licences.txt
└── build-info.json
```

Roughly **400 MB** with scanned-document support, **90 MB** without.

### 1.2 Build it

On a **Windows** machine with internet:

```powershell
git clone <repo> pdf2csv
cd pdf2csv
powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -Zip
```

> **Build on Windows.** Python wheels are platform-specific and a set collected
> on Linux will not install on the target. The script refuses to run elsewhere
> rather than producing a bundle that fails on delivery day.

For a deployment that will only ever see digital PDFs, drop the OCR stack:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -NoOcr -Zip
```

The light build starts faster and is a quarter of the size. Scanned pages then
produce a clear "this copy cannot read scanned documents" message rather than
failing.

The script finishes by running the bundle's own `pdf2csv check`. If that fails,
the build fails — a broken `._pth`, a missing DLL or a half-installed dependency
is caught here rather than on the client's desktop.

### 1.3 Test it before you hand it over

**This is the only test that means anything:**

1. Copy the folder to a Windows machine that has **never had this project on
   it**.
2. **Disable the network adapter.**
3. Double-click `Start PDF2CSV.bat`.
4. Convert a real document, end to end, and download the CSV.

A cached model or an already-installed package on your build machine hides a
first-run download perfectly. You will not catch it any other way.

The full list is in [`PRE_DELIVERY.md`](PRE_DELIVERY.md).

### 1.4 Hand it over

Give them the folder (or the zip) and point them at `READ ME FIRST.txt`.

If you send a zip, tell them to **extract the whole folder**. Dragging files out
of the Windows zip viewer one at a time leaves pieces behind, and this is the
single most common way a delivery fails on arrival.

### 1.5 Two things to settle with the client early

**Endpoint protection.** The bundle runs an unsigned executable that binds a
local port. Many corporate security products block exactly that. Confirm it is
allowed **before** delivery day — this is the most common late blocker on a
project like this, and it is an IT ticket, not a code change.

**Where output goes.** By default, copies are kept in the bundle's `output`
folder. If they want them on a shared drive, set it in the launcher:

```bat
set "PDF2CSV_OUTPUT=\\fileserver\finance\extracts"
```

---

## 2. Development checkout

Needs Python 3.10–3.12 on Windows, macOS or Linux.

```bash
git clone <repo> pdf2csv
cd pdf2csv

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"
```

### 2.1 Adding scanned-document support

`rapidocr-onnxruntime` declares a hard dependency on `opencv-python` — the GUI
build — which silently overwrites `opencv-python-headless`. Both packages own
the same `cv2` module, so whichever installs last wins, and the GUI build drags
a Qt stack onto a machine that is supposed to be minimal.

Install in two steps so the headless build survives:

```bash
pip install -r requirements-ocr.txt
pip install --no-deps -r requirements-ocr-nodeps.txt
```

Confirm it worked:

```bash
python -m pdf2csv check
```

`cv2` should report a `4.x` version. If it reports `5.x`, the GUI build won.

> The OCR models ship **inside the wheel** — 4.7 MB detection, 10.9 MB
> recognition, 0.6 MB orientation. Nothing is downloaded on first run, which is
> what makes the scanned path work on an air-gapped desktop.

### 2.2 Generate the fixtures and run the tests

```bash
python tests/fixtures/make_fixtures.py
pytest
```

The fixture PDFs are committed, so this is only needed after changing
`make_fixtures.py`. They are built by a small pure-Python PDF writer in
`tests/fixtures/pdfgen.py` — no ReportLab, and no binary blobs with no source.

Skip the slow OCR round trip while iterating:

```bash
pytest -m "not ocr"
```

### 2.3 Run it

```bash
python -m pdf2csv ui          # the interface, opens a browser
python -m pdf2csv convert statement.pdf
python -m pdf2csv check       # environment report
python -m pdf2csv cache clear # drop cached OCR results
```

---

## 3. Configuration

Everything is an environment variable, so the batch launchers can be edited
without touching Python.

| Variable | Default | What it does |
|---|---|---|
| `PDF2CSV_HOME` | the repo, or the bundle folder | Where logs, cache and output live |
| `PDF2CSV_OUTPUT` | `<home>\output` | Where finished files are kept |
| `PDF2CSV_PORT` | `8730` | Falls forward automatically if taken |
| `PDF2CSV_HOST` | `127.0.0.1` | **Do not change.** See below. |
| `PDF2CSV_MAX_UPLOAD_MB` | `200` | Upload ceiling |
| `PDF2CSV_MAX_PAGES` | `500` | Refuse absurd documents rather than appearing to hang |
| `PDF2CSV_OCR_DPI` | `300` | The floor for statement fonts |
| `PDF2CSV_LOW_CONFIDENCE` | `0.80` | Below this, a scanned figure is flagged |
| `PDF2CSV_CACHE` | `1` | Cache OCR results by file hash |
| `PDF2CSV_RETAIN_JOBS` | `40` | Jobs kept on disk before the oldest is swept |
| `PDF2CSV_LOG_LEVEL` | `INFO` | |
| `PDF2CSV_OPEN_BROWSER` | `1` | |

> **On `PDF2CSV_HOST`:** loopback is deliberate. Changing it to `0.0.0.0`
> exposes a client's financial documents to their office network with no
> authentication, and turns a desktop tool into something their security team
> has to review. There is no reason to.

---

## 4. Troubleshooting

**"The python folder is missing."**
The bundle was copied incompletely — almost always a zip extracted by dragging
files out of the viewer. Extract the whole folder.

**The window flashes and closes.**
Run `Check installation.bat` instead; it holds the window open and prints an
environment report.

**"Scanned documents cannot be read on this install."**
A `-NoOcr` build, or the OCR extra is missing from a development checkout. See
§2.1.

**Nothing opens in the browser.**
The URL is printed in the black window. Paste it in manually. If the port was
taken, the tool has already moved to the next free one and the printed URL
reflects that.

**`cv2` reports version 5.x in `pdf2csv check`.**
The GUI OpenCV build won. Reinstall:

```bash
pip uninstall -y opencv-python opencv-python-headless
pip install "opencv-python-headless>=4.10,<5"
```

**It is slow.**
Check `pdf2csv check` — if the document has scanned pages, 10–60 seconds each is
expected. Re-running the same file is near-instant because OCR results are
cached by content hash.
