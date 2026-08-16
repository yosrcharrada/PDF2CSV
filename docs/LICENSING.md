# Licensing

This software is delivered commercially to clients who will run it inside their
own organisation. That single fact decides several technical choices, and it is
worth writing down why, because the reasoning is invisible in the code and the
rejected alternatives are all more popular than the ones chosen.

---

## The constraint

**No AGPL or GPL code may enter the shipped set.**

The AGPL in particular is incompatible with this delivery model: it extends its
obligations to network-accessible use, and this application binds a local HTTP
port. Whether that constitutes "network use" is a question nobody wants to be
answering on a client's behalf, and the safe answer is to not ship AGPL code.

Everything delivered is **MIT, BSD-3-Clause, Apache-2.0** or equivalently
permissive.

---

## What this eliminated

These are the choices a reasonable engineer would otherwise make, and why they
were not available.

| Rejected | Licence | Chosen instead |
|---|---|---|
| **PyMuPDF** (`fitz`) | AGPL-3.0 | `pypdfium2` (BSD-3) |
| **camelot-py** | MIT itself, but pulls in **Ghostscript** (AGPL) | `pdfplumber` (MIT) |
| **PaddleOCR** | Apache-2.0, but a very heavy dependency tree | `rapidocr-onnxruntime` (Apache-2.0) |

PyMuPDF is the obvious choice for PDF rendering — it is fast, well maintained
and pleasant to use. It is also AGPL, and a commercial licence for it is a
per-deployment cost. `pypdfium2` wraps Google's PDFium under BSD-3 and needs no
system binary.

Camelot is the obvious choice for table extraction and is itself MIT. Its
lattice mode shells out to Ghostscript, which is AGPL. A transitive dependency
is still a dependency.

---

## The shipped set

| Package | Licence | Why |
|---|---|---|
| `pdfplumber` | MIT | Text and table extraction |
| `pypdfium2` | BSD-3-Clause | Rasterising; pure wheel, no system binary |
| `pandas` | BSD-3-Clause | Dataframes |
| `numpy` | BSD-3-Clause | Arrays |
| `openpyxl` | MIT | Excel export |
| `PyYAML` | MIT | Document profiles |
| `fastapi` | MIT | Web framework |
| `uvicorn` | BSD-3-Clause | ASGI server |
| `python-multipart` | Apache-2.0 | Upload parsing |

With the OCR extra:

| Package | Licence | Why |
|---|---|---|
| `rapidocr-onnxruntime` | Apache-2.0 | OCR, models included in the wheel |
| `onnxruntime` | MIT | Inference |
| `opencv-python-headless` | Apache-2.0 | Line detection, deskew |
| `Pillow` | MIT-CMU | Image handoff |
| `pyclipper` | BSL-1.0 | Polygon offsetting |
| `shapely` | BSD-3-Clause | Geometry |
| `six` | MIT | Compatibility shim |
| `tqdm` | MPL-2.0 + MIT | Progress bars (unused, pulled in transitively) |

### On `tqdm`

`tqdm` is dual-licensed MPL-2.0 and MIT. MPL-2.0 is file-level copyleft: it
obliges you to publish modifications *to those files*, and does not reach the
software that merely imports them. Unmodified and used as a library, it is safe
for commercial delivery. It is listed here because "MPL" in a licence report
looks alarming until someone checks, and the check should not have to be
repeated every time.

### On the OCR models

The PaddleOCR-derived ONNX weights shipped inside `rapidocr-onnxruntime` are
Apache-2.0, the same as the package. They ship inside the wheel — nothing is
downloaded at run time — which is also what makes the scanned path work on an
air-gapped machine.

---

## Auditing before handover

`packaging/build_portable.ps1` runs this automatically and writes
`licences.txt` into the bundle. To run it by hand:

```bash
pip install pip-licenses
pip-licenses --format=plain --with-urls
```

Look for `GPL` and `AGPL`. `LGPL` is not a problem for dynamic linking, and
flagging it every build teaches people to skip the check.

**Do this per release, not once.** Packages change licence. A dependency that
was BSD when you chose it can be relicensed in a minor version, and the first
you will hear about it is from the client's legal team.

---

## This project's own licence

MIT — see [`LICENSE`](../LICENSE). It imposes nothing on the client beyond
retaining the copyright notice.

---

## What is not covered here

This is an engineering note, not legal advice. Confirm the position with
whoever is accountable for it in your organisation before a commercial
handover — particularly if the deployment differs from the one assumed
throughout this document: a single analyst, on a single desktop, running it
against their own employer's documents.
