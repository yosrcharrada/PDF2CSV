# PDF2CSV — technical overview

A briefing for people who did not build it: what it is, what it runs on, why
each piece was chosen, how it stays offline, and what still has to be decided
before it is deployed.

Written for a review with IT and security. Sections 5, 6 and 7 are the ones
that meeting will spend its time on.

---

## 1. What the tool does

An analyst receives certificate-of-deposit paperwork as PDF — usually a scan,
sometimes a photograph of a scan — and has to retype it into a 36-column CSV
that another system imports. That retyping is slow, and it is where the errors
come from.

The tool takes the PDF and produces the CSV.

It handles two document classes, and the difference between them drove most of
the engineering:

| | Declaration | Fiche du souscripteur |
|---|---|---|
| Subscribers | One | Several, one row each |
| Layout | Labels and values | A ruled grid |
| Pages | One | Columns split across two pages |
| Output | One row | One row per subscriber |

A third path handles ordinary tables — bank statements, ledgers — for documents
that are neither.

### What makes this harder than it sounds

The CSV is **not a transcription of the table**. Only five facts come off the
page: issuer, rate, quantity, and two dates. The other seventeen columns are
*derived* from them by business rules the document never mentions.

So accuracy has to hold for five values rather than for every cell. That is a
much smaller promise, and it survives a 200 DPI scan comfortably. It also moves
the risk: the danger is in the mapping rules, not in the OCR. That is exactly
where the real errors turned out to be (§4).

---

## 2. Tech stack, and why each piece

Everything shipped is MIT, BSD-3 or Apache-2.0. **No GPL or AGPL.** That was a
hard constraint from the start, and it ruled out several otherwise obvious
choices.

| Layer | Package | Licence | What it does |
|---|---|---|---|
| PDF text | `pdfplumber` | MIT | Reads text and tables where a text layer exists |
| PDF raster | `pypdfium2` | BSD-3 | Renders pages to images. Pure wheel — **no system binary to install** |
| OCR | `rapidocr-onnxruntime` | Apache-2.0 | Recognition. Weights ship inside the package (§5) |
| ML runtime | `onnxruntime` | MIT | Runs the OCR models. CPU only |
| Imaging | `opencv-python-headless` | Apache-2.0 | Deskew, rotation, ink analysis. **Headless** — no GUI libraries |
| Data | `pandas`, `numpy` | BSD-3 | Tabular manipulation |
| Excel | `openpyxl` | MIT | `.xlsx` export, and reading the ISIN workbook |
| Web | `fastapi`, `uvicorn` | MIT / BSD-3 | The local web interface |
| Upload | `python-multipart` | Apache-2.0 | Multipart parsing |

### What was rejected, and why

| Rejected | Reason | Chosen instead |
|---|---|---|
| **PyMuPDF** (`fitz`) | AGPL-3.0. The network clause is a real problem for anything that binds an HTTP port | `pypdfium2` |
| **camelot-py** | MIT itself, but pulls in Ghostscript (AGPL) | `pdfplumber` |
| **PaddleOCR** | Apache-2.0, but a very heavy dependency tree | `rapidocr-onnxruntime` |
| **Tesseract / Poppler** | Need a system-level install, which defeats "no admin rights" | Bundled wheels only |

Worth raising in the meeting explicitly: AGPL applied to something serving HTTP
is a genuine exposure, and avoiding it shaped the entire stack rather than being
a detail at the end.

---

## 3. How it works, end to end

```
PDF  ->  is there a text layer?
          |-- yes -> read positioned text directly (fast, exact)
          |-- no  -> render page -> deskew -> OCR -> positioned text
                                |
                 which document is this? (declaration / fiche / ordinary table)
                                |
                 extract the five facts -> apply the mapping rules
                                |
                 allocate an ISIN -> run the checks -> CSV + Excel + report
```

### The parts that needed real work

**Columns come from the printed ruling lines.** The obvious approach is to infer
columns from where the headings sit. Headings turned out to be unreliable: they
wrap onto two lines, they merge with their neighbour, they sit anywhere inside
their cell. A ruling line is one unambiguous position. We find them by
projecting ink onto the horizontal axis and taking columns that are ink for 85%
of the table's height. On the sample this recovers all twelve instrument columns
and all seven identity columns exactly — including a blank spacer column that
also appears in the finance team's own spreadsheet.

**Orientation is resolved by parsing, not by guessing.** Every page arrives
rotated a quarter turn and the PDF metadata says it is not. Two heuristics were
implemented and both picked wrong on at least one real page, because the
recogniser silently corrects upside-down text. Instead the tool tries each
rotation and keeps whichever one *parses* — a page read the wrong way round does
not produce a date under a date heading.

**Deskew before recognition.** The scans are tilted about three degrees.
Invisible to a reader; fatal to row grouping, because a row's text drifts about
a hundred pixels across the page while the rows are only eighty apart. Without
correction the rows interleave.

**Checks annotate, they never block.** Every run produces a report. A failed
check flags the output rather than suppressing it. A tool that refuses to
produce a file gets worked around; a file that says which row to look at gets
checked.

---

## 4. Problems found, and how they were fixed

The honest list, including the ones we caused.

### Mapping rules that were wrong

Three rules produced values that looked entirely ordinary and were incorrect.
None would have been caught by reading the output. All three were settled by
diffing the finance team's own reference CSVs against the documents those CSVs
were produced from.

| Rule | Was | Is | Consequence if unfixed |
|---|---|---|---|
| `nominalValueAllotted` | 500 × quantity | The montant printed (500 000 a certificate) | **Every row out by a factor of 1000** |
| `numberOfCertificates` | The printed quantity | montant ÷ unit price | Wrong wherever the document contradicts itself |
| `auctionDate`, `issueDate`, `startDate` | The subscription date | The date the document is dated | Three date columns, whenever the two differ |

The middle one deserves a note for the meeting: **the source document is
internally inconsistent.** It prints a quantity of 5 beside an amount of
3 500 000 at 500 000 each — which is 7. The finance team's reference row says 7.
The tool follows the amount, keeps the printed quantity in its own column, and
raises a check saying the two disagree. Neither reading wins silently.

### The one column that cannot be produced

`code` is the subscriber's securities account. It appears in neither document
class. It also determines `BIC` and `amountToBePaid`. The tool leaves it empty,
defaults the other two to settlement with the issuer — correct for three of the
four reference rows — and says so in the report on every run.

**This needs an answer from finance:** where does that account number come from?

### Recognition problems

| Problem | Fix |
|---|---|
| Neighbouring cells merged into one string | Re-read that cell from its own pixels, cut at the ruling line. Cutting the *string* proportionally lands mid-word |
| Spaces dropped between printed capitals | Re-read identity cells at 2× magnification, where they read as written |
| Headings misrecognised (`lnteret` for `Interet`, clipped endings) | Approximate matching, with the threshold set so the one dangerous confusion — a maturity date answering for a subscription date — scores 0.33 against a 0.82 bar |
| A wrapped cell read as a row of its own | Rows are identified by width: a real row has content in most columns, a wrapped fragment in one or two |

### Two regressions we caused, both caught

Included because they show what the test suite does and does not catch.

**The delimiter broke silently.** The CSV must be semicolon-delimited, because
the values contain commas — the receiving system reads a comma-decimal locale.
The delimiter was chosen by comparing a profile's *name* to `"declaration"`.
Adding the fiche reader introduced a second profile producing the same layout,
the comparison missed it, and every fiche exported comma-delimited. It now tests
whether the standard columns are *present*, and there is a test for it. The old
check had none, which is precisely why breaking it made no noise.

**Source columns were being dropped.** The 36-column layout folds the instrument
and the rate into a single field. Read literally, that meant a document could
state a libellé, a rate, an amount and a quantity and have none of them appear
anywhere in the output as written. A value read and then discarded is
unauditable. All fourteen source columns are now appended after the 36 — the
first 36 stay byte-identical to the reference files, because whatever imports
them reads by position.

### A data-handling mistake, disclosed

While documenting why `code` cannot be derived, four **real client
securities-account numbers** and two subscriber names were written into
`docs/DECLARATIONS.md` and pushed to a public repository. They are now masked
and the fix is pushed.

**They remain in the repository's git history.** Removing them means rewriting
published history, which is a decision for this meeting rather than one to take
quietly. Treat the values as disclosed. See §7.

---

## 5. How it runs offline

The requirement: **internet at setup only.** After that, nothing.

### There is no network code

No HTTP client calls anywhere in the application. No `requests`, no `urllib`, no
telemetry, no analytics, no update check, no licence check. The only socket the
process opens is the one it listens on.

### The OCR models ship inside the package

This is the usual reason a tool like this fails on an isolated machine — the
first run tries to fetch model weights. It cannot here:

```
ch_PP-OCRv4_det_infer.onnx            4.7 MB   text detection
ch_PP-OCRv4_rec_infer.onnx           10.9 MB   text recognition
ch_ppocr_mobile_v2.0_cls_infer.onnx   0.6 MB   orientation
                                     -------
                                     16.2 MB   inside the installed wheel
```

They are verified present, by name and size, before OCR runs.

### Three things that would phone home, and what stops each

| Would reach the network | Stopped by |
|---|---|
| FastAPI's `/docs` — Swagger UI loads from a CDN | `docs_url=None`. `/docs` returns 404, asserted by a test |
| A remote font, script or image in the page | `Content-Security-Policy: default-src 'self'` on every response |
| HuggingFace-style model loaders | `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, set by the launcher |

A test asserts that **no `http://` or `https://` reference exists anywhere** in
the shipped HTML, CSS or JavaScript. The one exception is the SVG XML namespace,
which is an identifier and not a fetch.

### Verified rather than assumed

A clean Windows machine with the network adapter disabled, end to end, is a
pre-delivery blocker. A cached model or a package already present on the build
machine hides a first-run download perfectly.

---

## 6. Security posture

| Concern | Position |
|---|---|
| **Network exposure** | Binds `127.0.0.1` only. Not reachable from the network |
| **Authentication** | None, by design — single-user desktop application on loopback |
| **Data leaving the machine** | None. Documents read locally, results written locally |
| **CSP** | `default-src 'self'`, `base-uri 'none'`, `frame-ancestors 'none'`, plus per-directive limits on script, style, image, font and connect |
| **Other headers** | `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` |
| **Upload validation** | Extension *and* magic bytes (`%PDF`). 200 MB and 500-page ceilings, both configurable |
| **Path traversal** | Filenames reduced to their basename, directory components discarded. Output folders keyed by a random job id |
| **Retention** | Jobs capped (40 by default); uploads deleted on cleanup |
| **Admin rights** | Not required, at install or at run time |
| **Licences** | MIT / BSD / Apache-2.0 only. No copyleft |

### Questions to expect, and the honest answer

- **"Is there authentication?"** No. It is loopback-only and single-user. That
  is a deliberate scope decision, not an oversight — and it is the one thing
  that must change if the deployment model ever changes.
- **"What happens to the documents?"** They stay on the machine. Uploads are
  removed on cleanup; outputs are kept where the analyst can find them.
- **"Can the output be audited?"** Yes. Every run writes a validation report
  beside the CSV, and the document's own printed values are exported next to
  the derived ones, so any row can be checked against the paper without
  reopening the PDF.
- **"What is the supply chain?"** Nine runtime packages plus the OCR stack, all
  permissive licences, pinned by range and frozen to exact versions in a lock
  file at build time.
- **"Has any client data been exposed?"** Yes — see §4. Be ready for this one.

---

## 7. Deployment, and what is still open

### Two delivery routes

**Portable bundle — intended for analysts.** An embeddable Python interpreter,
the application and the OCR weights in one folder. No Python installation, no
admin rights, no internet. Copy the folder, double-click a `.bat` file.

Embeddable Python was chosen over PyInstaller deliberately: PyInstaller fights
with ONNX runtime's native libraries and fails at runtime with an opaque missing
import. The bundle sets `PYTHONNOUSERSITE=1` so it cannot accidentally import
packages from the build machine's *or* the client machine's user site-packages —
a failure mode that stays invisible until it isn't.

**Repository checkout — for developers.** Clone, create a virtual environment,
two-step install. The two steps exist because `rapidocr-onnxruntime` requires
the GUI build of OpenCV, which would overwrite the headless one; both own the
`cv2` module. Verified working from a clean clone.

### Open decisions

| # | Decision | Why it matters |
|---|---|---|
| 1 | **Where does `code` come from?** | Three columns cannot be completed without it |
| 2 | **Rewrite the git history** to remove the disclosed account numbers? | Public repository. Masked at HEAD, still in history |
| 3 | **Should the repository be public at all?** | It currently is. Nothing about the tool requires it |
| 4 | **Columns 23–36: filled or blank** when the document names a subscriber? | They are filled. The finance team's own reference file leaves them empty |
| 5 | **Will the receiving system accept 50 columns?** | The first 36 are unchanged and in order, so trimming is safe if not |
| 6 | **Who allocates ISINs?** | The one with a silent failure mode — see below |

### The ISIN allocation problem

Codes come from a finite workbook that ships with the project. Which codes have
been consumed is recorded **per machine**, in a ledger that cannot be committed
because it changes on every run.

So two people working from their own copies will be handed **the same ISINs**,
and neither will see anything wrong. Running out fails loudly. Colliding does
not.

That is safe for one analyst on one desktop, which is what this was built for.
It is not safe for two. Before a second person issues codes, one of these has to
be true:

- one machine does all the allocation, or
- the ledger lives on a shared drive and every copy points at it, or
- each machine gets its own block of codes in its own workbook.

The second option is a small change. It needs deciding before rollout, not
after.

---

## 8. State of the work

- 303 automated tests, passing
- Both reference documents reproduce field for field, apart from the three
  columns that need the account number
- No client document is used in any test — the grid logic is tested against a
  table the tests draw themselves
- Lint clean
- Verified working from a clean clone, fresh environment, fresh install
