"""ISIN allocation — the one part of this pipeline that carries state.

Every other field is a pure function of the document. This one consumes a code
from a finite pool, permanently, and that difference drives the whole design.

Three properties matter more than convenience:

**Idempotency.** Re-processing the same declaration must return the *same*
ISIN, never a fresh one. Without it a double-click silently burns a code and
puts the register out of step with the market — a failure with no symptom at
the time and no way to reconstruct afterwards. Allocation is therefore keyed on
the content of the declaration, not on when it was processed.

**A ledger separate from the workbook.** The workbook cannot be the record of
what was used: an analyst reordering rows or re-saving it would change the
answer. The ledger is append-only and authoritative; the workbook only supplies
candidates.

**Loud exhaustion.** When a sheet runs out the export fails. It never falls
through to a blank or a reused code, because a duplicate ISIN is worse than a
missing file — the file gets noticed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pdf2csv.config import get_settings
from pdf2csv.declarations.mapping import DeclarationFacts, Issuer, issuer_from_title
from pdf2csv.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "AllocationError",
    "IsinLedger",
    "IsinPool",
    "PoolExhausted",
    "allocation_key",
    "discover_pool",
]

_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[A-Z0-9]$")
_HEADER_WORDS = ("bloc", "isin")


POOL_DIRNAME = "isin"
"""Folder searched for the workbook, beside the application."""


def discover_pool() -> Path | None:
    """Find the ISIN workbook without being told where it is.

    Passing a path on every run is a step that gets forgotten, and a forgotten
    pool means a row exported with an empty ISIN — which looks like output and
    is not usable. Searched in order:

    1. ``PDF2CSV_ISIN_POOL``, so a deployment can point anywhere;
    2. an ``isin`` folder beside the application or the bundle;
    3. the working directory.

    Any ``.xlsx`` in those folders is accepted, preferring one whose name
    mentions ISIN, so the workbook can be dropped in under whatever name the
    finance team gave it. Returns ``None`` when nothing is found, which the
    caller reports as a check rather than an error.
    """
    explicit = os.environ.get("PDF2CSV_ISIN_POOL", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    roots: list[Path] = []
    try:
        home = get_settings().home
        roots += [home / POOL_DIRNAME, home]
    except Exception:
        pass

    package_root = Path(__file__).resolve().parent.parent.parent
    roots += [
        package_root / POOL_DIRNAME,
        package_root.parent / POOL_DIRNAME,
        Path.cwd() / POOL_DIRNAME,
    ]

    for root in roots:
        try:
            if not root.is_dir():
                continue
            books = sorted(root.glob("*.xlsx"))
        except OSError:
            continue
        if not books:
            continue
        # Prefer a name that mentions ISIN; otherwise take the first.
        named = [b for b in books if "isin" in b.name.casefold()]
        chosen = (named or books)[0]
        log.info("found ISIN workbook at %s", chosen)
        return chosen

    return None


class AllocationError(RuntimeError):
    """Raised when an ISIN cannot be allocated. Always fatal to an export."""


class PoolExhausted(AllocationError):
    """A client's sheet has no unused codes left."""


def _normalise(name: str) -> str:
    """Collapse whitespace and case for matching issuer names to sheet names.

    Excel truncates sheet names at 31 characters, so
    ``COMPAGNIE INTERNATIONALE DE LEASING EMETTEUR CD`` is stored as
    ``COMPAGNIE INTERNATIONALE DE LEA``. Sheet names also carry stray double
    spaces (``ATTIJARI LEASING  CD``). Matching has to survive both.
    """
    return re.sub(r"\s+", " ", str(name)).strip().casefold()


def allocation_key(facts: DeclarationFacts) -> str:
    """Stable identity for one issuance.

    Deliberately built from the *instrument* — issuer, rate and both dates —
    rather than from a hash of the file. Two subscribers to the same issuance
    arrive as separate declarations and must share one ISIN, which a file hash
    would never give them. It also means a re-scan of the same declaration at a
    different resolution still maps to the same code.
    """
    issuer = issuer_from_title(facts.title)
    parts = (
        issuer.short,
        f"{facts.taux:.4f}",
        facts.date_souscription.isoformat(),
        facts.date_remboursement.isoformat(),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{issuer.short}-{digest}"


# --------------------------------------------------------------------------- #
# The pool
# --------------------------------------------------------------------------- #


@dataclass
class IsinPool:
    """The workbook of available codes, one sheet per issuer."""

    path: Path
    _sheets: dict[str, list[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> IsinPool:
        import openpyxl

        source = Path(path)
        if not source.is_file():
            raise AllocationError(f"The ISIN workbook was not found at {source}.")

        workbook = openpyxl.load_workbook(source, data_only=True, read_only=True)
        sheets: dict[str, list[str]] = {}
        try:
            for worksheet in workbook.worksheets:
                codes: list[str] = []
                for row in worksheet.iter_rows(values_only=True):
                    for cell in row:
                        if cell is None:
                            continue
                        text = str(cell).strip().upper()
                        if not text or any(w in text.casefold() for w in _HEADER_WORDS):
                            continue
                        if _ISIN_PATTERN.match(text) and text not in codes:
                            codes.append(text)
                sheets[_normalise(worksheet.title)] = codes
        finally:
            workbook.close()

        log.info(
            "loaded ISIN pool: %d sheet(s), %d code(s)",
            len(sheets),
            sum(len(v) for v in sheets.values()),
        )
        return cls(path=source, _sheets=sheets)

    def codes_for(self, issuer: Issuer) -> list[str]:
        """Codes on the sheet belonging to this issuer, in sheet order.

        Matched on the issuer's full name against the sheet name, allowing for
        Excel's 31-character truncation.
        """
        target = _normalise(issuer.name)
        exact = self._sheets.get(target)
        if exact is not None:
            return exact

        for name, codes in self._sheets.items():
            if target.startswith(name) or name.startswith(target):
                return codes

        raise AllocationError(
            f"The ISIN workbook has no sheet for {issuer.name!r}. "
            f"Sheets present: {', '.join(sorted(self._sheets)) or 'none'}."
        )

    @property
    def sheet_names(self) -> list[str]:
        return sorted(self._sheets)


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


@dataclass
class IsinLedger:
    """Append-only record of which code was issued to which issuance."""

    path: Path
    _by_key: dict[str, dict] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> IsinLedger:
        source = Path(path)
        entries: dict[str, dict] = {}
        if source.is_file():
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
                for entry in payload.get("allocations", []):
                    entries[entry["key"]] = entry
            except (OSError, ValueError, KeyError) as exc:
                # Refuse to continue rather than silently start a fresh ledger:
                # doing so would reissue every code already in circulation.
                raise AllocationError(
                    f"The ISIN ledger at {source} could not be read ({exc}). "
                    "Allocation has been stopped so that codes already issued "
                    "are not reused. Restore it from backup before continuing."
                ) from exc
        return cls(path=source, _by_key=entries)

    @property
    def used(self) -> set[str]:
        return {entry["isin"] for entry in self._by_key.values()}

    def existing(self, key: str) -> str | None:
        entry = self._by_key.get(key)
        return entry["isin"] if entry else None

    def record(self, key: str, isin: str, *, note: str = "") -> None:
        self._by_key[key] = {
            "key": key,
            "isin": isin,
            "allocated_at": dt.datetime.now().astimezone().isoformat(),
            "note": note,
        }
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "allocations": sorted(self._by_key.values(), key=lambda e: e["allocated_at"]),
        }
        # Write and replace, so an interrupted save cannot truncate the record
        # of codes already issued.
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)


# --------------------------------------------------------------------------- #
# Allocation
# --------------------------------------------------------------------------- #


def allocate(
    facts: DeclarationFacts,
    pool: IsinPool,
    ledger: IsinLedger,
    *,
    note: str = "",
) -> tuple[str, bool]:
    """Return ``(isin, was_already_allocated)`` for this issuance.

    Idempotent: the same issuance always returns the same code, and the ledger
    is only written the first time.
    """
    key = allocation_key(facts)

    existing = ledger.existing(key)
    if existing is not None:
        log.info("reusing ISIN %s for %s", existing, key)
        return existing, True

    issuer = issuer_from_title(facts.title)
    candidates = pool.codes_for(issuer)
    used = ledger.used

    for code in candidates:
        if code not in used:
            ledger.record(key, code, note=note or facts.title)
            log.info("allocated ISIN %s to %s", code, key)
            return code, False

    raise PoolExhausted(
        f"Every ISIN on the {issuer.name!r} sheet has been used "
        f"({len(candidates)} in total). Add a new block to the workbook before "
        "processing more declarations for this issuer."
    )


def allocation_check(isin: str, reused: bool) -> dict[str, object]:
    """Allocation as a validation check, alongside the arithmetic ones."""
    return {
        "id": "isin_allocated",
        "title": "An ISIN was assigned from the pool",
        "passed": bool(isin),
        "detail": (
            f"{isin} (already allocated to this issuance — not consumed again)"
            if reused
            else f"{isin} (newly consumed from the pool)"
        ),
    }
