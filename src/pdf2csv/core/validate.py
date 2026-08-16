"""Reconciliation checks — the gate that makes the CSV trustworthy.

The rule this module exists to enforce: **the CSV is not the deliverable, the
CSV plus its report is.** Unreconciled finance output is worse than no output,
because it looks authoritative.

The second rule: **failures annotate, they never block.** An analyst who cannot
get their data out will export it some other way and lose the report entirely.
An analyst handed the data plus "these two rows break the running balance" goes
and looks at those two rows. Loud and specific beats obstructive.

Checks are written to be *skippable*: when a document does not contain the
information a check needs, the check does not run and says so, rather than
failing. A check that fails whenever it cannot find its inputs is noise, and
noise trains people to ignore the report.
"""

from __future__ import annotations

import math
import re

import pandas as pd

from pdf2csv.core.normalize import AMOUNT, NormalizedTable
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import DocumentMeta, PageKind, Severity, ValidationReport
from pdf2csv.profiles import Profile
from pdf2csv.wording import count, listed, plural

log = get_logger(__name__)

TOL = 0.01
"""Currency rounding tolerance, in the document's own units."""

_MAX_LISTED = 5
"""How many offending rows to name in a check detail before saying 'and N more'."""

# Header patterns used when a profile has not declared its column roles.
_BALANCE_RE = re.compile(r"\b(balance|solde|closing|bal)\b", re.IGNORECASE)
_DEBIT_RE = re.compile(r"\b(debit|d[ée]bit|withdrawal|withdrawn|paid\s*out|dr)\b", re.IGNORECASE)
_CREDIT_RE = re.compile(r"\b(credit|cr[ée]dit|deposit|paid\s*in|cr)\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\b(amount|montant|value|net)\b", re.IGNORECASE)


def _tolerance(reference: float) -> float:
    """Absolute tolerance, widened slightly for very large sums."""
    return TOL + abs(reference) * 1e-9


def _find_column(table: NormalizedTable, pattern: re.Pattern[str]) -> str | None:
    """First amount column whose header matches, or ``None``."""
    for spec in table.columns:
        if spec.kind == AMOUNT and pattern.search(spec.name):
            return spec.name
    return None


def _resolve_roles(table: NormalizedTable, profile: Profile) -> dict[str, str | None]:
    """Work out which columns play which reconciliation role.

    A profile's declaration always wins over header guessing, because a profile
    was written by someone looking at the actual document.
    """
    declared = profile.balance_columns or {}
    resolved: dict[str, str | None] = {}
    names = {spec.name.casefold(): spec.name for spec in table.columns}

    for role, pattern in (
        ("closing", _BALANCE_RE),
        ("debit", _DEBIT_RE),
        ("credit", _CREDIT_RE),
        ("amount", _AMOUNT_RE),
    ):
        stated = declared.get(role) or declared.get("closing" if role == "closing" else role)
        if stated and stated.casefold() in names:
            resolved[role] = names[stated.casefold()]
        else:
            resolved[role] = _find_column(table, pattern)

    # A credit column must not also be the balance column: "CR" matches both.
    if resolved["credit"] and resolved["credit"] == resolved["closing"]:
        resolved["credit"] = None
    if resolved["debit"] and resolved["debit"] == resolved["closing"]:
        resolved["debit"] = None

    return resolved


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #


def check_table_found(table: NormalizedTable, report: ValidationReport, profile: Profile) -> None:
    rows = len(table.frame)
    report.add(
        "table_found",
        "A table was found and rows were extracted",
        rows >= max(1, profile.min_rows),
        severity=Severity.ERROR,
        detail=f"{count(rows, 'data row')} extracted.",
        hint=(
            ""
            if rows >= max(1, profile.min_rows)
            else "No table was recognised. If the PDF is a scan, check that the OCR "
            "add-on is installed; otherwise this layout is not yet supported."
        ),
    )


def check_expected_columns(
    table: NormalizedTable, report: ValidationReport, profile: Profile
) -> None:
    if not profile.expected_columns:
        return
    present = {c.name.casefold() for c in table.columns}
    missing = [c for c in profile.expected_columns if c.casefold() not in present]
    report.add(
        "expected_columns",
        "All columns this document format should have are present",
        not missing,
        severity=Severity.ERROR,
        detail=(
            "All expected columns found."
            if not missing
            else f"Missing: {', '.join(missing)}. Found: {', '.join(c.name for c in table.columns)}"
        ),
        hint="" if not missing else "The layout may have changed, or a column ran off the page.",
    )


def check_numeric_parsing(table: NormalizedTable, report: ValidationReport) -> None:
    """Guide check 5 — no unreadable values left in numeric columns."""
    failures = [f for f in report.flags if f.reason == "could not be read as a number"]
    where = listed(
        [f"row {f.row + 1} {f.column} ({f.value})" for f in failures], limit=_MAX_LISTED
    )

    report.add(
        "numeric_parsing",
        "Every value in the number columns could be read",
        not failures,
        severity=Severity.ERROR,
        detail=(
            "Every figure was read cleanly."
            if not failures
            else f"{count(len(failures), 'cell')} could not be read: {where}"
        ),
        hint=(
            ""
            if not failures
            else f"{plural(len(failures), 'This cell is', 'These cells are')} blank in the "
            "CSV. Check them against the PDF before using it."
        ),
    )


def check_ocr_confidence(
    table: NormalizedTable, report: ValidationReport, meta: DocumentMeta
) -> None:
    """Guide check 6 — low-confidence OCR in numeric cells is the top review flag."""
    if not any(kind is PageKind.SCANNED for kind in table.row_kinds):
        return

    low = [f for f in report.flags if "confidence" in f.reason]
    repaired = [f for f in report.flags if f.reason.startswith("OCR read this as")]

    detail_parts = []
    if low:
        detail_parts.append(f"{count(len(low), 'figure')} recognised with low confidence")
    if repaired:
        detail_parts.append(f"{count(len(repaired), 'cell')} corrected automatically")
    if not detail_parts:
        detail_parts.append("Every scanned figure was recognised confidently")

    report.add(
        "ocr_confidence",
        "Scanned figures were read clearly",
        not low,
        severity=Severity.WARNING,
        detail="; ".join(detail_parts) + ".",
        hint=(
            ""
            if not low
            else "Highlighted cells came from a scan and may be misread. "
            "A sharper scan at 300 DPI or more usually fixes this."
        ),
    )


def check_stated_totals(table: NormalizedTable, report: ValidationReport) -> None:
    """Guide check 1 — the document's own totals must equal the extracted rows.

    This is the strongest signal available. If the statement says the debits
    come to 48,201.55 and the extracted rows come to 47,100.30, a row is
    missing, and no amount of visual inspection of a 400-row CSV would find it.
    """
    if not table.stated_totals:
        return

    mismatches: list[str] = []
    for column, stated in table.stated_totals.items():
        if column not in table.frame.columns:
            continue
        values = pd.to_numeric(table.frame[column], errors="coerce")

        # A totals row does not state a *sum* for a balance column — it states
        # the closing balance. Summing every intermediate balance and comparing
        # that to the closing figure would fail on every correctly extracted
        # statement ever produced, so balances are compared as closing figures.
        if _BALANCE_RE.search(column):
            trailing = values.dropna()
            if trailing.empty:
                continue
            computed = float(trailing.iloc[-1])
            label = "closing balance"
        else:
            computed = float(values.sum(skipna=True))
            label = "extracted rows total"

        if not math.isclose(computed, stated, abs_tol=_tolerance(stated)):
            mismatches.append(
                f"{column}: document says {stated:,.2f}, {label} is "
                f"{computed:,.2f} (off by {computed - stated:,.2f})"
            )

    report.add(
        "stated_totals",
        "Column totals match the totals printed in the document",
        not mismatches,
        severity=Severity.ERROR,
        detail=(
            f"Reconciled {count(len(table.stated_totals), 'stated total')}."
            if not mismatches
            else "; ".join(mismatches)
        ),
        hint=(
            ""
            if not mismatches
            else "A difference here almost always means rows were missed or double-counted. "
            "Do not use this CSV until it is explained."
        ),
    )


def check_running_balance(
    table: NormalizedTable, report: ValidationReport, profile: Profile
) -> None:
    """Guide check 3, generalised — every row's balance must follow from the last.

    Rather than only checking opening + movements = closing (which passes even
    when two rows in the middle are swapped or one is duplicated and another
    dropped), this walks the ledger row by row. It localises the break to a
    specific row number, which is what makes it actionable.

    The sign convention is not assumed. Both are evaluated and the one that fits
    the document better is used, because "debit increases the balance" is a
    genuine convention in some ledgers and getting it backwards would report
    every single row as broken.
    """
    roles = _resolve_roles(table, profile)
    balance_col = roles["closing"]
    if not balance_col or balance_col not in table.frame.columns:
        return

    frame = table.frame
    balance = pd.to_numeric(frame[balance_col], errors="coerce")
    if balance.notna().sum() < 3:
        return  # too little to say anything meaningful

    debit = (
        pd.to_numeric(frame[roles["debit"]], errors="coerce").fillna(0.0)
        if roles["debit"] and roles["debit"] in frame.columns
        else None
    )
    credit = (
        pd.to_numeric(frame[roles["credit"]], errors="coerce").fillna(0.0)
        if roles["credit"] and roles["credit"] in frame.columns
        else None
    )
    amount = (
        pd.to_numeric(frame[roles["amount"]], errors="coerce").fillna(0.0)
        if roles["amount"] and roles["amount"] in frame.columns
        else None
    )

    if debit is not None or credit is not None:
        movement = (credit if credit is not None else 0.0) - (
            debit if debit is not None else 0.0
        )
    elif amount is not None:
        movement = amount
    else:
        return

    best_breaks: list[int] | None = None
    best_sign = 1
    for sign in (1, -1):
        breaks = _balance_breaks(balance, movement * sign)
        if best_breaks is None or len(breaks) < len(best_breaks):
            best_breaks, best_sign = breaks, sign

    assert best_breaks is not None
    checked = int(balance.notna().sum()) - 1

    where = listed([f"row {r + 1}" for r in best_breaks], limit=_MAX_LISTED)

    for row in best_breaks:
        report.flag(
            row,
            balance_col,
            "balance does not follow from the previous row",
            severity=Severity.ERROR,
        )

    report.add(
        "running_balance",
        "Each row's balance follows from the row before it",
        not best_breaks,
        severity=Severity.ERROR,
        detail=(
            f"Checked {count(checked, 'consecutive row pair')}; all consistent"
            + (" (debits increase the balance)." if best_sign == -1 else ".")
            if not best_breaks
            else f"{count(len(best_breaks), 'row')} "
            f"{plural(len(best_breaks), 'breaks', 'break')} the running balance: {where}"
        ),
        hint=(
            ""
            if not best_breaks
            else "A break usually means a row was missed, duplicated, or its amount misread. "
            f"Compare {plural(len(best_breaks), 'that row', 'those rows')} against the PDF."
        ),
    )


def _balance_breaks(balance: pd.Series, movement: pd.Series) -> list[int]:
    """Row indices where ``balance[i] != balance[i-1] + movement[i]``."""
    breaks: list[int] = []
    previous: float | None = None
    for index in range(len(balance)):
        current = balance.iloc[index]
        if pd.isna(current):
            continue
        if previous is not None:
            step = movement.iloc[index] if index < len(movement) else 0.0
            if pd.isna(step):
                step = 0.0
            expected = previous + float(step)
            if not math.isclose(float(current), expected, abs_tol=_tolerance(expected)):
                breaks.append(index)
        previous = float(current)
    return breaks


def check_debits_equal_credits(
    table: NormalizedTable, report: ValidationReport, profile: Profile
) -> None:
    """Guide check 2 — for journals, total debits must equal total credits.

    Only run when the document looks like a journal: debit and credit columns
    present and no running balance column. A bank statement has debits and
    credits that are *not* meant to be equal, and asserting otherwise would
    fail every statement ever produced.
    """
    roles = _resolve_roles(table, profile)
    debit_col, credit_col, balance_col = roles["debit"], roles["credit"], roles["closing"]

    if not (debit_col and credit_col):
        return
    if balance_col and not profile.balance_columns:
        return  # looks like a statement, not a journal

    frame = table.frame
    debits = float(pd.to_numeric(frame[debit_col], errors="coerce").sum(skipna=True))
    credits = float(pd.to_numeric(frame[credit_col], errors="coerce").sum(skipna=True))
    balanced = math.isclose(debits, credits, abs_tol=_tolerance(debits))

    report.add(
        "debits_equal_credits",
        "Total debits equal total credits",
        balanced,
        severity=Severity.ERROR,
        detail=(
            f"debits {debits:,.2f} vs credits {credits:,.2f} "
            f"(difference {debits - credits:,.2f})"
        ),
        hint="" if balanced else "An unbalanced journal means at least one entry is incomplete.",
    )


def check_pages_contributed(
    table: NormalizedTable, report: ValidationReport, meta: DocumentMeta
) -> None:
    """Guide check 4 — every page that held a table put rows into the output.

    Catches the failure where header de-duplication or a layout change eats an
    entire page's worth of rows. Silent, and otherwise invisible in a 400-row
    CSV.
    """
    if not table.row_pages:
        return

    contributed = set(table.row_pages)
    expected = {
        index + 1
        for index, kind in enumerate(meta.page_kinds)
        if kind is not PageKind.EMPTY
    }
    # Only pages between the first and last contributing page are candidates —
    # cover letters and terms pages at either end legitimately hold no table.
    first, last = min(contributed), max(contributed)
    interior = {p for p in expected if first <= p <= last}
    missing = sorted(interior - contributed)

    report.add(
        "pages_contributed",
        "No page inside the table was skipped",
        not missing,
        severity=Severity.WARNING,
        detail=(
            f"Rows came from {plural(len(contributed), 'page', 'pages')} "
            f"{listed([str(p) for p in sorted(contributed)], limit=8)}."
            if not missing
            else f"{plural(len(missing), 'Page', 'Pages')} "
            f"{listed([str(p) for p in missing], limit=8)} "
            f"{plural(len(missing), 'sits', 'sit')} inside the table but produced no rows."
        ),
        hint=(
            ""
            if not missing
            else f"{plural(len(missing), 'That page', 'Those pages')} may use a different "
            "layout, or the rows may have been mistaken for repeated headers."
        ),
    )


def check_duplicate_rows(table: NormalizedTable, report: ValidationReport) -> None:
    """Identical rows are sometimes real and sometimes a double-read page."""
    frame = table.frame
    if frame.empty:
        return
    duplicated = frame.duplicated(keep="first")
    duplicates = int(duplicated.sum())
    if duplicates:
        for row in frame.index[duplicated][:_MAX_LISTED]:
            report.flag(
                int(row),
                frame.columns[0],
                "identical to an earlier row",
                severity=Severity.WARNING,
            )

    report.add(
        "duplicate_rows",
        "No repeated rows that might be double-counted",
        duplicates == 0,
        severity=Severity.WARNING,
        detail=(
            "No duplicate rows."
            if duplicates == 0
            else f"{count(duplicates, 'row')} "
            f"{plural(duplicates, 'is an exact duplicate', 'are exact duplicates')} "
            "of an earlier row."
        ),
        hint=(
            ""
            if duplicates == 0
            else "Two identical transactions on the same day are legitimate. "
            "A whole repeated block means a page was read twice."
        ),
    )


def note_extraction_summary(
    table: NormalizedTable, report: ValidationReport, meta: DocumentMeta
) -> None:
    """Not a test — the audit trail, recorded as a check so it lands in the sidecar."""
    parts = [
        count(len(table.frame), "row"),
        count(len(table.columns), "column"),
        f"{count(meta.n_digital, 'digital page')}",
        f"{count(meta.n_scanned, 'scanned page')}",
    ]
    if table.total_row_count:
        parts.append(
            f"{count(table.total_row_count, 'totals row')} held back for reconciliation"
        )
    if table.repaired_cells:
        parts.append(f"{count(table.repaired_cells, 'scanned cell')} corrected automatically")

    report.add(
        "extraction_summary",
        "Extraction summary",
        True,
        severity=Severity.INFO,
        detail="; ".join(parts) + ".",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def run_all(
    table: NormalizedTable,
    report: ValidationReport,
    meta: DocumentMeta,
    profile: Profile,
) -> ValidationReport:
    """Run every applicable check against a normalised table.

    ``report`` arrives carrying the cell flags raised during normalisation, so
    checks can consult them rather than re-deriving what already went wrong.
    """
    check_table_found(table, report, profile)
    check_expected_columns(table, report, profile)
    check_numeric_parsing(table, report)
    check_ocr_confidence(table, report, meta)
    check_stated_totals(table, report)
    check_running_balance(table, report, profile)
    check_debits_equal_credits(table, report, profile)
    check_pages_contributed(table, report, meta)
    check_duplicate_rows(table, report)
    note_extraction_summary(table, report, meta)

    log.info("validation: %s", report.summary())
    return report
