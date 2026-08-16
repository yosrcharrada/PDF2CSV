# PDF to CSV — how to use it

One page. Keep it next to the tool.

---

## Starting it

Double-click **`Start PDF2CSV.bat`**.

A black window opens and stays open — that is the program running, not an
error. Your browser opens a page called *PDF to CSV*.

**Leave the black window alone while you work.** Closing it stops the program.

---

## Converting a document

1. **Drag a PDF onto the page**, or click to choose one.
2. **Wait.** Most documents take a second or two. Scanned documents take about
   ten seconds per page, because the text has to be recognised from a picture.
   The progress bar tells you which page it is on.
3. **Read the result banner at the top.**
4. **Click Download CSV.**

---

## What the result banner means

| Banner | What it means | What to do |
|---|---|---|
| **All checks passed** | The totals printed in your document match the rows that were extracted. | Use the file. |
| **Worth a quick look** | The totals add up, but some cells are uncertain — usually figures read from a scan. | Check the highlighted cells against the PDF, then use it. |
| **Checks did not pass** | Something does not reconcile. | **Do not use the file** until you understand why. Read on. |

---

## When a check does not pass

The failed check is shown first and already open. It tells you what went wrong
and which rows are involved, and the same rows are highlighted in the table
below it.

The three you are most likely to see:

**"Column totals match the totals printed in the document"**
The document says one figure and the extracted rows add up to another. Almost
always a row was missed or counted twice. The message gives you the difference —
look for a transaction of exactly that amount.

**"Each row's balance follows from the row before it"**
The running balance breaks at a specific row. Compare that row against the PDF.
A missing row, a duplicated row, or a misread amount all cause this.

**"Every value in the number columns could be read"**
A cell could not be interpreted as a number and is blank in your CSV. The
message names the row and shows what the PDF actually contained.

If the document itself is fine and the tool is simply reading it wrongly, that
is a change request — send the file to whoever supports the tool.

---

## Highlighted cells

| Colour | Meaning |
|---|---|
| **Amber** | Read from a scan with low confidence, or corrected automatically. Worth confirming. |
| **Red** | Could not be read, or breaks the running balance. Needs attention. |

Hover over any highlighted cell to see the reason and what the PDF showed.

The Excel download keeps these highlights, with the reason as a cell comment.

---

## Where your files go

Downloads go wherever your browser normally puts them — usually **Downloads**.

A copy is also kept in the **`output`** folder next to `Start PDF2CSV.bat`,
along with a small report file recording whether that conversion reconciled.
That means a spreadsheet found on a shared drive weeks later can still be traced
back to whether its numbers were ever checked.

---

## Things worth knowing

- **Nothing leaves your computer.** No upload, no cloud, no internet.
- **Checks catch arithmetic, not meaning.** A column that adds up correctly but
  was labelled wrongly in the original PDF passes every check.
- **Scan quality drives result quality.** Crooked, faint or low-resolution scans
  produce worse results, and the tool tells you when it is not confident.
- **Unfamiliar layouts may not work.** A new bank's template is a change
  request, not a fault.
- **Password-protected PDFs** must have the password removed in your PDF reader
  first.

---

## If something goes wrong

1. Double-click **`Check installation.bat`**.
2. Copy everything it prints.
3. Send that, plus the file **`logs\pdf2csv.log`**, to support.

Neither contains any of your document's contents — only file names, page counts
and the results of the checks.

**It will not start at all?** The folder was probably copied incompletely.
If it came as a zip, extract the *whole folder* rather than dragging files out
of the zip viewer.

---

## Who to contact

| | |
|---|---|
| **Tool support / new document formats** | _fill in before handover_ |
| **Where output CSVs should be filed** | _fill in before handover_ |
| **Escalation** | _fill in before handover_ |
