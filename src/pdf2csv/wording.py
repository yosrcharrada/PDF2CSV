"""Phrasing helpers for text an analyst reads.

Small module, real purpose. Every string in the validation report is read by
someone who did not ask for this software and will not read a manual, and
``1 row(s) break the running balance`` tells them two things: what happened,
and that this was written by a programmer for a programmer. The second one
costs trust that the first one needs.
"""

from __future__ import annotations

__all__ = ["count", "listed", "plural"]


def plural(quantity: int, singular: str, plural_form: str | None = None) -> str:
    """Return the right form of a word for a quantity. No parenthesised 's'.

    >>> plural(1, "row")
    'row'
    >>> plural(3, "row")
    'rows'
    >>> plural(2, "match", "matches")
    'matches'
    """
    if abs(quantity) == 1:
        return singular
    return plural_form if plural_form is not None else f"{singular}s"


def count(quantity: int, singular: str, plural_form: str | None = None) -> str:
    """A quantity and its noun, with thousands separators.

    >>> count(1, "row")
    '1 row'
    >>> count(1400, "row")
    '1,400 rows'
    """
    return f"{quantity:,} {plural(quantity, singular, plural_form)}"


def listed(items: list[str], limit: int = 5) -> str:
    """Join items for reading, truncating politely once the list gets long.

    >>> listed(["row 2", "row 5"])
    'row 2 and row 5'
    >>> listed([f"row {n}" for n in range(1, 9)], limit=3)
    'row 1, row 2, row 3 and 5 more'
    """
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) <= limit:
        return f"{', '.join(items[:-1])} and {items[-1]}"
    remaining = len(items) - limit
    return f"{', '.join(items[:limit])} and {remaining} more"
