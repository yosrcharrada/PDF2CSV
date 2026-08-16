# Pre-delivery checklist

Work through this before handing the bundle to a client. Items marked
**BLOCKER** have sunk projects like this one at the last minute.

---

## Technical

- [ ] **BLOCKER — Runs end to end on a clean Windows machine with the network
      adapter disabled.**
      Not the build machine. Not a VM that once had Python on it. A machine that
      has never had this project. A cached model or an installed package hides a
      first-run download perfectly, and this is the only way to catch it.

- [ ] Runs without admin rights, from a standard user account.

- [ ] Runs from the location it will actually live in — a desktop folder, a
      network share, a USB stick. Confirm the `output` folder is writable there.

- [ ] `Check installation.bat` reports no problems, and its "Python isolation"
      section shows all import paths inside the installation.

- [ ] A real client document converts correctly, end to end, and the CSV opens
      in their Excel with accented characters intact.

- [ ] A deliberately broken document produces a failed check. If everything
      always passes, the gate is not working and every green tick is worthless.

- [ ] `pytest` passes on the build machine, including the OCR round trip.

- [ ] `licences.txt` reviewed — no GPL or AGPL in the shipped set. See
      [`LICENSING.md`](LICENSING.md).

- [ ] `installed-packages.txt` and `build-info.json` are in the bundle, so
      "what exactly did we ship?" has an answer in six months.

- [ ] Rotating log confirmed working in `logs/`, and confirmed to contain **no
      document contents** — only filenames, page counts and check results.

---

## With the client, early

Start these conversations at the beginning of the project, not the week of
delivery. Each one is somebody else's queue.

- [ ] **BLOCKER — Endpoint protection allows an unsigned executable to bind a
      local port.**
      This is the single most common late blocker. The bundle runs an unsigned
      `python.exe` from a user folder and opens a loopback socket, which is a
      pattern many corporate security products block by default. It is an IT
      ticket with a lead time, not a code change.

- [ ] Confirmed that input PDFs may sit on local disk while being processed.
      Some data-handling policies say otherwise, and that changes the design.

- [ ] Agreed where output CSVs are filed. If it is a shared drive, set
      `PDF2CSV_OUTPUT` in the launcher before handover.

- [ ] Agreed data retention. The bundle sweeps documents after 40 jobs by
      default; confirm that is acceptable, or set `PDF2CSV_RETAIN_JOBS`.

- [ ] Named owner for post-handoff issues — new statement formats, changed bank
      templates. Written into the runbook, not agreed verbally.

- [ ] Agreed what happens when a document fails validation. Who is called, and
      what they are expected to do.

---

## Documentation

- [ ] `READ ME FIRST.txt` is in the bundle root.

- [ ] `Runbook.md` contact details are **filled in**. It ships with three
      placeholders and they are easy to miss.

- [ ] The client has been given [`SUPPORTED_FORMATS.md`](SUPPORTED_FORMATS.md),
      or its contents in a form they will read. In particular they have been
      told, in writing:
      - unseen layouts are a change request, not a fault
      - scan quality drives output quality
      - **the checks catch arithmetic, not meaning**

- [ ] Someone other than the author has followed the runbook from scratch,
      on a real machine, without help.

---

## Handover

- [ ] Bundle delivered as a folder or a zip, with the instruction to **extract
      the whole folder**. Dragging files out of the Windows zip viewer is the
      most common way a delivery arrives broken.

- [ ] Version, build date and build machine recorded (`build-info.json`).

- [ ] Repository tagged at the commit the bundle was built from. Without this,
      reproducing a client's exact build later is guesswork.

- [ ] A short session with the analyst — fifteen minutes, converting one of
      their own documents, including one that fails validation so they have seen
      what that looks like before it matters.

---

## After delivery

- [ ] Confirm within a week that they have actually used it. A tool that is
      installed and unused usually failed at a step nobody was told about.

- [ ] Ask for the first document that surprised them. That document is the next
      fixture.
