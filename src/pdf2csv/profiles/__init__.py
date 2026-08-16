"""Document profiles — per-format knowledge, kept out of the code.

A new bank template is a configuration change, not a development task, for as
long as the difference is *describable*: which column holds the closing
balance, whether the document uses comma decimals, what the totals row is
called. Those go in a YAML file here and the extractor stays untouched.

When a format differs structurally rather than descriptively — a three-level
nested header, amounts split across two physical columns — that is genuine
development work, and pretending otherwise by growing the profile schema
without limit is how configuration systems turn into bad programming languages.
The line is drawn deliberately: profiles describe, they do not compute.

Profiles are matched against the document's first-page text by keyword. The
``generic`` profile always matches and always loses to a more specific one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf2csv.logging_setup import get_logger

log = get_logger(__name__)

_PROFILE_DIR = Path(__file__).parent


@dataclass
class Profile:
    """Everything the pipeline can be told about one document format."""

    name: str = "generic"
    description: str = "Default behaviour — everything inferred from the document."

    match_keywords: list[str] = field(default_factory=list)
    """Case-insensitive phrases sought in the first pages. All must appear."""

    # --- Parsing -------------------------------------------------------------
    decimal_separator: str | None = None
    """``"."`` or ``","``. ``None`` infers it from the document's own amounts."""

    dayfirst: bool | None = None
    """``True`` for 04/03/2025 = 4 March. ``None`` infers it."""

    fill_merged_labels: bool = False
    """Forward-fill blank cells in label columns caused by merged cells.

    Off by default and deliberately so. When a document genuinely uses merged
    row labels this is exactly right; when it has legitimately blank cells it
    fabricates data that reconciles perfectly and is wrong. Turn it on per
    format, once you have looked at that format.
    """

    identifier_columns: list[str] = field(default_factory=list)
    """Header patterns to keep as text even when they look numeric.

    Account numbers, cheque numbers and references are digits that must never
    become floats: ``0041`` would lose its leading zeros and a 16-digit card
    number would arrive in Excel as ``1.23457E+15``.
    """

    date_columns: list[str] = field(default_factory=list)
    amount_columns: list[str] = field(default_factory=list)
    """Header patterns to force to a type when inference is unreliable."""

    # --- Validation ----------------------------------------------------------
    total_row_labels: list[str] = field(
        default_factory=lambda: ["total", "totals", "grand total", "sub total", "subtotal"]
    )
    """Row labels that state a total the extracted rows must reconcile against."""

    balance_columns: dict[str, str] = field(default_factory=dict)
    """Maps the roles ``opening``/``debit``/``credit``/``closing`` to headers."""

    expected_columns: list[str] = field(default_factory=list)
    """If set, a missing column becomes a failed check rather than a surprise."""

    min_rows: int = 1
    """Fewer extracted rows than this fails the 'did we find the table' check."""

    # ------------------------------------------------------------------------
    def matches(self, text: str) -> bool:
        if not self.match_keywords:
            return False
        haystack = text.casefold()
        return all(keyword.casefold() in haystack for keyword in self.match_keywords)

    def is_identifier(self, header: str) -> bool:
        return _any_pattern_matches(header, self.identifier_columns)

    def forces_date(self, header: str) -> bool:
        return _any_pattern_matches(header, self.date_columns)

    def forces_amount(self, header: str) -> bool:
        return _any_pattern_matches(header, self.amount_columns)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            log.warning(
                "profile %r has unrecognised key(s): %s",
                data.get("name", "?"),
                ", ".join(sorted(unknown)),
            )
        return cls(**{k: v for k, v in data.items() if k in known})


def _any_pattern_matches(header: str, patterns: list[str]) -> bool:
    target = header.casefold().strip()
    for pattern in patterns:
        try:
            if re.search(pattern, target, flags=re.IGNORECASE):
                return True
        except re.error:  # a malformed pattern in YAML is a substring test
            if pattern.casefold() in target:
                return True
    return False


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

# Built in rather than shipped as YAML, so the tool still works if the profile
# directory is missing from a badly assembled bundle.
GENERIC = Profile(
    name="generic",
    identifier_columns=[
        r"\b(account|acct|iban|swift|bic)\b",
        r"\b(ref|reference|cheque|check|voucher|invoice|receipt)\s*(no|num|number|#)?\b",
        r"\b(id|code)\b",
        r"^no\.?$",
    ],
)


def load_profiles() -> list[Profile]:
    """Read every YAML profile shipped alongside this module."""
    profiles: list[Profile] = []
    for path in sorted(_PROFILE_DIR.glob("*.yaml")):
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            log.warning("could not read profile %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            log.warning("profile %s is not a mapping — ignored", path.name)
            continue
        data.setdefault("name", path.stem)
        profiles.append(Profile.from_dict(data))
    return profiles


def select_profile(document_text: str, *, requested: str | None = None) -> Profile:
    """Choose the profile for a document.

    An explicit request always wins, so an analyst who knows the format can
    override a bad guess. Otherwise the most specific keyword match wins, and
    ``generic`` is the floor.
    """
    available = load_profiles()

    if requested:
        for profile in available:
            if profile.name.casefold() == requested.casefold():
                return profile
        if requested.casefold() != "generic":
            log.warning("requested profile %r not found — falling back to generic", requested)
        return GENERIC

    matched = [p for p in available if p.matches(document_text)]
    if not matched:
        return GENERIC

    # Most keywords = most specific.
    best = max(matched, key=lambda p: len(p.match_keywords))
    log.info("matched document profile %r", best.name)
    return best
