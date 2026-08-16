"""Turning a stitched grid of strings into a typed, reconcilable table.

Three jobs happen here, in an order that matters:

1. **Type inference per column**, document-wide rather than cell by cell, so
   ``1.234`` is read the same way on page 9 as on page 1.
2. **OCR digit repair**, applied *only* to cells in a column already
   established as numeric, and *only* to rows that came from OCR. Applying it
   more widely turns ``"Bloomberg LP"`` into ``"8loomberg LP"``.
3. **Separating stated totals from data rows.** A ``TOTAL`` row is not a
   transaction. Left in the dataframe it double-counts every column sum; simply
   deleted, the document's own arithmetic is thrown away. Instead it is lifted
   out and kept as the figure the extracted rows must reconcile against — which
   is what makes the validation stage able to say anything at all.

The type rules exist to protect against two specific, expensive mistakes:
identifiers becoming floats (``0041`` → ``41``, a 16-digit card number →
``1.23457E+15``), and unparseable cells vanishing into blanks without anyone
being told.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from pdf2csv.core.amounts import (
    infer_dayfirst,
    infer_decimal_separator,
    is_blank_marker,
    parse_amount,
    parse_date,
    repair_ocr_digits,
)
from pdf2csv.core.stitch import StitchedTable
from pdf2csv.logging_setup import get_logger
from pdf2csv.models import PageKind, Severity, ValidationReport
from pdf2csv.profiles import GENERIC, Profile

log = get_logger(__name__)

TEXT = "text"
AMOUNT = "amount"
DATE = "date"

# A column must be this consistently parseable before it is retyped. Below it,
# the column stays text and nothing is lost.
_TYPE_THRESHOLD = 0.70

_ID_HEADER = re.compile(
    r"\b(account|acct|iban|swift|bic|ref|reference|cheque|check|voucher|invoice|"
    r"receipt|id|code|no|num|number)\b",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"^\d{5,}$")
_LEADING_ZERO = re.compile(r"^0\d+$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class ColumnSpec:
    """The decision made about one column, kept for the UI and the report."""

    index: int
    name: str
    kind: str
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "reason": self.reason}


@dataclass
class NormalizedTable:
    """A typed table plus everything validation needs to reconcile it."""

    frame: pd.DataFrame
    columns: list[ColumnSpec] = field(default_factory=list)
    stated_totals: dict[str, float] = field(default_factory=dict)
    """Totals the *document* claims, lifted out of the data rows."""

    total_row_count: int = 0
    row_pages: list[int] = field(default_factory=list)
    row_kinds: list[PageKind] = field(default_factory=list)
    repaired_cells: int = 0

    @property
    def amount_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.kind == AMOUNT]

    @property
    def date_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.kind == DATE]

    @property
    def text_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.kind == TEXT]


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #


def clean_headers(header: list[str]) -> list[str]:
    """Make headers non-empty, whitespace-normalised and unique.

    Original wording is preserved rather than snake_cased: the analyst is going
    to open this in Excel next to the PDF, and ``Débit`` matching ``Débit`` is
    worth more than a tidy identifier.
    """
    names: list[str] = []
    seen: dict[str, int] = {}

    for index, raw in enumerate(header):
        name = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not name:
            name = f"column_{index + 1}"

        key = name.casefold()
        if key in seen:
            seen[key] += 1
            name = f"{name} ({seen[key]})"
        else:
            seen[key] = 1
        names.append(name)

    return names


# --------------------------------------------------------------------------- #
# Column typing
# --------------------------------------------------------------------------- #


def _is_identifier(values: list[str], header: str, profile: Profile) -> bool:
    """Should this column stay text despite looking numeric?"""
    if profile.is_identifier(header):
        return True

    filled = [v for v in values if v.strip() and not is_blank_marker(v)]
    if not filled:
        return False

    # Leading zeros are proof: no amount is written 0041.
    if sum(1 for v in filled if _LEADING_ZERO.match(v.strip())) / len(filled) >= 0.3:
        return True

    # Long unbroken digit runs with no separator are references, not money.
    long_digits = sum(1 for v in filled if _LONG_DIGITS.match(v.strip())) / len(filled)
    if long_digits >= 0.8 and _ID_HEADER.search(header):
        return True
    # Without a corroborating header, demand near-unanimity: a column of large
    # round amounts would otherwise be mistaken for a column of references.
    return long_digits >= 0.95


def infer_column_kind(
    values: list[str],
    header: str,
    profile: Profile,
    *,
    decimal_sep: str | None,
    dayfirst: bool | None,
) -> tuple[str, str]:
    """Decide a column's type. Returns ``(kind, human_readable_reason)``."""
    if profile.forces_date(header):
        return DATE, f"profile {profile.name!r} declares this a date column"
    if profile.forces_amount(header):
        return AMOUNT, f"profile {profile.name!r} declares this an amount column"

    filled = [v for v in values if v.strip() and not is_blank_marker(v)]
    if not filled:
        return TEXT, "column is empty"

    if _is_identifier(filled, header, profile):
        return TEXT, "looks like an identifier — kept as text to preserve digits"

    dates = sum(1 for v in filled if parse_date(v, dayfirst=dayfirst) is not None)
    if dates / len(filled) >= _TYPE_THRESHOLD:
        return DATE, f"{dates}/{len(filled)} values parsed as dates"

    amounts = sum(1 for v in filled if parse_amount(v, decimal_sep) is not None)
    if amounts / len(filled) >= _TYPE_THRESHOLD:
        return AMOUNT, f"{amounts}/{len(filled)} values parsed as amounts"

    return TEXT, "values are not consistently numeric or dated"


# --------------------------------------------------------------------------- #
# Totals rows
# --------------------------------------------------------------------------- #


def _normalise_label(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.casefold()).strip()


def _is_total_row(row: list[str], profile: Profile) -> bool:
    """Is this row the document stating a total rather than a transaction?

    Requires the label cell to be *essentially* the total keyword. Without that
    constraint a legitimate transaction described as "Total fees debited in
    March" is pulled out of the data and the CSV silently loses a row.
    """
    for cell in row:
        label = _normalise_label(cell)
        if not label:
            continue
        for keyword in profile.total_row_labels:
            key = _normalise_label(keyword)
            if not key or key not in label:
                continue
            remainder = label.replace(key, "", 1).strip()
            if len(remainder) <= 20:
                return True
    return False


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def normalize_table(
    table: StitchedTable,
    report: ValidationReport,
    *,
    profile: Profile = GENERIC,
    low_confidence: float = 0.80,
) -> NormalizedTable:
    """Type, clean and reconcile-prepare one stitched table."""
    headers = clean_headers(table.header)
    n_cols = len(headers)
    rows = table.rows

    # --- Document-wide locale decisions, made once ---------------------------
    all_cells = [cell for row in rows for cell in row]
    decimal_sep = profile.decimal_separator or infer_decimal_separator(all_cells)
    dayfirst = profile.dayfirst if profile.dayfirst is not None else infer_dayfirst(all_cells)
    log.info(
        "locale: decimal separator %s, dayfirst %s",
        decimal_sep or "auto",
        "auto" if dayfirst is None else dayfirst,
    )

    # --- Optional forward-fill for merged label cells ------------------------
    if profile.fill_merged_labels:
        rows = _forward_fill_labels(rows, headers, profile, decimal_sep=decimal_sep)

    # --- Split stated totals away from data rows -----------------------------
    data_rows: list[list[str]] = []
    data_pages: list[int] = []
    data_kinds: list[PageKind] = []
    data_confidences: list[list[float]] = []
    total_rows: list[list[str]] = []

    for index, row in enumerate(rows):
        if _is_total_row(row, profile):
            total_rows.append(row)
            continue
        data_rows.append(row)
        data_pages.append(table.row_pages[index] if index < len(table.row_pages) else 0)
        data_kinds.append(
            table.row_kinds[index] if index < len(table.row_kinds) else PageKind.DIGITAL
        )
        if table.row_confidences is not None and index < len(table.row_confidences):
            data_confidences.append(table.row_confidences[index])
        else:
            data_confidences.append([1.0] * n_cols)

    # --- Type each column ----------------------------------------------------
    columns: list[ColumnSpec] = []
    for column_index, name in enumerate(headers):
        values = [row[column_index] if column_index < len(row) else "" for row in data_rows]
        kind, reason = infer_column_kind(
            values, name, profile, decimal_sep=decimal_sep, dayfirst=dayfirst
        )
        columns.append(ColumnSpec(index=column_index, name=name, kind=kind, reason=reason))

    # --- Build the typed frame ----------------------------------------------
    frame_data: dict[str, list] = {}
    repaired_total = 0

    for spec in columns:
        raw_values = [
            row[spec.index] if spec.index < len(row) else "" for row in data_rows
        ]

        if spec.kind == AMOUNT:
            parsed, repaired = _build_amount_column(
                raw_values,
                spec,
                data_kinds,
                data_confidences,
                report,
                decimal_sep=decimal_sep,
                low_confidence=low_confidence,
            )
            repaired_total += repaired
            frame_data[spec.name] = parsed
        elif spec.kind == DATE:
            frame_data[spec.name] = _build_date_column(
                raw_values, spec, report, dayfirst=dayfirst
            )
        else:
            frame_data[spec.name] = [v.strip() for v in raw_values]

    frame = pd.DataFrame(frame_data)

    # --- What the document said the totals were ------------------------------
    stated = _extract_stated_totals(total_rows, columns, decimal_sep=decimal_sep)

    if repaired_total:
        log.info("OCR digit repair rescued %d numeric cell(s)", repaired_total)

    return NormalizedTable(
        frame=frame,
        columns=columns,
        stated_totals=stated,
        total_row_count=len(total_rows),
        row_pages=data_pages,
        row_kinds=data_kinds,
        repaired_cells=repaired_total,
    )


def _build_amount_column(
    raw_values: list[str],
    spec: ColumnSpec,
    row_kinds: list[PageKind],
    row_confidences: list[list[float]],
    report: ValidationReport,
    *,
    decimal_sep: str | None,
    low_confidence: float,
) -> tuple[list[float | None], int]:
    """Parse a numeric column, repairing OCR damage and flagging what remains."""
    parsed: list[float | None] = []
    repaired = 0

    for row_index, raw in enumerate(raw_values):
        value = parse_amount(raw, decimal_sep)

        from_ocr = row_index < len(row_kinds) and row_kinds[row_index] is PageKind.SCANNED

        # Repair is confined to numeric columns and OCR-sourced rows, which is
        # the only context where a letter inside a number is a scanning error
        # rather than the actual content of the cell.
        if value is None and from_ocr and not is_blank_marker(raw) and raw.strip():
            candidate = parse_amount(repair_ocr_digits(raw), decimal_sep)
            if candidate is not None:
                value = candidate
                repaired += 1
                report.flag(
                    row_index,
                    spec.name,
                    f"OCR read this as {raw.strip()!r}; corrected to {candidate:,.2f}",
                    severity=Severity.WARNING,
                    value=raw.strip(),
                )

        if value is None and raw.strip() and not is_blank_marker(raw):
            report.flag(
                row_index,
                spec.name,
                "could not be read as a number",
                severity=Severity.ERROR,
                value=raw.strip(),
            )

        # A confidently-wrong digit is more dangerous than an unreadable one,
        # so low-confidence numeric cells are surfaced even when they parsed.
        if from_ocr and row_index < len(row_confidences):
            confidences = row_confidences[row_index]
            if spec.index < len(confidences) and confidences[spec.index] < low_confidence:
                report.flag(
                    row_index,
                    spec.name,
                    f"OCR confidence {confidences[spec.index]:.0%} — check this figure",
                    severity=Severity.WARNING,
                    value=raw.strip(),
                )

        parsed.append(value)

    return parsed, repaired


def _build_date_column(
    raw_values: list[str],
    spec: ColumnSpec,
    report: ValidationReport,
    *,
    dayfirst: bool | None,
) -> list[str | None]:
    """Parse a date column to ISO ``YYYY-MM-DD`` strings.

    Strings rather than datetimes: ISO text is unambiguous in the CSV, survives
    Excel's import heuristics without being reinterpreted, and sorts correctly
    as text.
    """
    out: list[str | None] = []
    for row_index, raw in enumerate(raw_values):
        parsed = parse_date(raw, dayfirst=dayfirst)
        if parsed is None:
            if raw.strip() and not is_blank_marker(raw):
                report.flag(
                    row_index,
                    spec.name,
                    "could not be read as a date",
                    severity=Severity.WARNING,
                    value=raw.strip(),
                )
            out.append(None)
        else:
            out.append(parsed.isoformat())
    return out


def _extract_stated_totals(
    total_rows: list[list[str]],
    columns: list[ColumnSpec],
    *,
    decimal_sep: str | None,
) -> dict[str, float]:
    """Read the figures out of the document's own totals rows."""
    stated: dict[str, float] = {}
    for row in total_rows:
        for spec in columns:
            if spec.kind != AMOUNT or spec.index >= len(row):
                continue
            value = parse_amount(row[spec.index], decimal_sep)
            if value is None:
                continue
            # Several totals rows (subtotal then grand total): the last wins,
            # which is the grand total in every layout observed so far.
            stated[spec.name] = value
    return stated


def _forward_fill_labels(
    rows: list[list[str]],
    headers: list[str],
    profile: Profile,
    *,
    decimal_sep: str | None,
) -> list[list[str]]:
    """Carry merged row labels down into the blank cells beneath them.

    Only applied to columns that are not numeric, and only when a profile has
    explicitly asked for it — see the warning on ``Profile.fill_merged_labels``.
    """
    if not rows:
        return rows

    numeric_columns = set()
    for index in range(len(headers)):
        values = [row[index] for row in rows if index < len(row) and row[index].strip()]
        if values and sum(
            1 for v in values if parse_amount(v, decimal_sep) is not None
        ) / len(values) >= _TYPE_THRESHOLD:
            numeric_columns.add(index)

    filled = [list(row) for row in rows]
    last_seen: dict[int, str] = {}
    for row in filled:
        for index, cell in enumerate(row):
            if index in numeric_columns:
                continue
            if cell.strip():
                last_seen[index] = cell
            elif index in last_seen:
                row[index] = last_seen[index]
    return filled
