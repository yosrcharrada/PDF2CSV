"""Parsing money and dates out of whatever a PDF happens to contain.

This module will cause more bugs than the rest of the codebase combined, so it
is written defensively and tested hard. The governing principle:

    **Returning ``None`` is always safer than returning a plausible wrong
    number.** An unparsed cell is visible in the validation report and gets
    looked at. A silently mangled one becomes a wrong figure in a client's
    accounts.

That is why, for example, ``"Page 1 of 12"`` returns ``None`` rather than
``112`` — a naive "strip everything that is not a digit" implementation gets
that wrong, and it is a real string that appears in real statements.

Locale handling is document-wide, not cell-by-cell. ``1.234`` is 1234 in a
French statement and 1.234 in an American one, and no amount of cleverness
resolves that from the single cell. :func:`infer_decimal_separator` looks at
every amount in the document, decides once, and the decision is then applied
uniformly.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter

__all__ = [
    "infer_dayfirst",
    "infer_decimal_separator",
    "is_blank_marker",
    "looks_numeric",
    "parse_amount",
    "parse_date",
    "repair_ocr_digits",
]

# --------------------------------------------------------------------------- #
# Character classes
# --------------------------------------------------------------------------- #

# Every space-like character a PDF might hand us. Regular spaces included:
# thousands separators are frequently plain spaces in French typesetting.
_SPACES = "      　\t\r\n\v\f"
_SPACE_TABLE = str.maketrans(dict.fromkeys(_SPACES, ""))

# Dash-like characters that mean "minus" when they lead a number.
_MINUSES = "−–—‐‑‒﹣－"

_CURRENCY_SYMBOLS = "$€£¥₹₽₩¢₪₦₺₫₴₡₱₲₵₸₼₾﷼¤"

# ISO codes and the informal abbreviations that show up in statements.
# Order matters only in that longer codes are tried first when stripping.
_CURRENCY_CODES = frozenset(
    {
        # Europe / North America
        "USD", "EUR", "GBP", "CHF", "CAD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON",
        # Middle East and North Africa
        "TND", "DT", "MAD", "DZD", "EGP", "LYD", "AED", "SAR", "QAR", "KWD", "BHD", "OMR",
        "JOD", "LBP", "ILS", "TRY",
        # Asia-Pacific
        "JPY", "CNY", "AUD", "NZD", "SGD", "HKD", "INR", "PKR", "BDT", "IDR", "MYR", "THB",
        "VND", "PHP", "KRW", "TWD",
        # Sub-Saharan Africa and Latin America
        "ZAR", "NGN", "KES", "GHS", "RUB", "UAH", "BRL", "MXN", "ARS", "CLP", "COP", "PEN",
        "UYU",
    }
)

# Markers that mean "nothing here", not "zero". Distinguishing these from junk
# matters: a nil marker is expected and should not be reported as a parse
# failure, while genuine junk in a numeric column should be.
_BLANK_MARKERS = frozenset(
    {
        "-", "--", "---", "–", "—", ".", "..", "...",
        "nil", "n/a", "na", "n.a.", "n.a", "none", "null", "void",
        "néant", "neant", "s/o", "sans objet",
        "0.00-", "x", "xx",
    }
)

# Bare "D" and "C" are deliberately absent. They appear in the wild, but they
# also form the last letter of USD, TND, MAD and several other currency codes,
# and stripping one turns "1,234.56 USD" into an unparseable "1,234.56US". A
# marker that occasionally helps is not worth a parser that rejects dollars.
_SIGN_SUFFIXES_NEGATIVE = ("DEBIT", "DÉBIT", "DR", "DB")
_SIGN_SUFFIXES_POSITIVE = ("CREDIT", "CRÉDIT", "CR", "CT")

_ADJACENT_TO_NUMBER = "0123456789).,"


# --------------------------------------------------------------------------- #
# Cheap predicates
# --------------------------------------------------------------------------- #


def _clean_spaces(value: str) -> str:
    return value.translate(_SPACE_TABLE)


def is_blank_marker(value: object) -> bool:
    """True for placeholders that mean 'no value' rather than a broken cell.

    Statements use a bare dash for nil far more often than they use ``0.00``.
    Treating it as a parse failure would light up the validation report with
    noise on perfectly clean documents.
    """
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.casefold() in _BLANK_MARKERS


def looks_numeric(value: object) -> bool:
    """A quick 'is this cell trying to be a number?' test.

    Used to decide whether OCR digit repair may touch a cell. It must never
    return True for a name or a description, because repairing digits inside
    ``"Bloomberg LP"`` would turn it into ``"8loomberg LP"``.
    """
    if value is None:
        return False
    text = _clean_spaces(str(value)).strip()
    if not text or is_blank_marker(text):
        return False

    digits = sum(1 for ch in text if ch.isdigit())
    if digits == 0:
        return False

    # Letters are allowed only if they form a recognised money marker.
    stripped, _ = _strip_sign_markers(text)
    letters = sum(1 for ch in stripped if ch.isalpha())
    if letters:
        return False

    return digits / max(len(text), 1) >= 0.3


# --------------------------------------------------------------------------- #
# Amount parsing
# --------------------------------------------------------------------------- #


def _strip_sign_markers(text: str) -> tuple[str, bool]:
    """Peel off accounting sign notation. Returns ``(remainder, is_negative)``.

    Runs to a fixed point, because notations stack: ``$(1,234.56)`` puts a
    symbol outside a bracket, and ``(1,234.56)DR`` puts a marker outside one.
    A single pass leaves the residue behind and the value is then rejected as
    non-numeric — silently, and for a value that was perfectly readable.

    An explicit DR/CR marker *sets* the sign rather than toggling it. Toggling
    makes ``(1,234.56) DR`` positive, which is a reading no accountant would
    recognise; both notations say "debit", and saying it twice does not mean
    the opposite.
    """
    s = text.strip()
    s = "".join("-" if ch in _MINUSES else ch for ch in s)

    explicit: int | None = None
    bracketed = False
    signed = False

    changed = True
    while changed and s:
        changed = False

        # Currency is peeled inside the same loop rather than before or after
        # it, because the two nest in both orders: "$(1,234.56)" wraps a symbol
        # around a bracket, "(1,234.56 USD)" wraps a bracket around a code.
        # Either ordering of two separate passes leaves one of them stranded.
        without_currency = _strip_currency(s)
        if without_currency != s:
            s = without_currency
            changed = True
            continue

        for open_ch, close_ch in (("(", ")"), ("[", "]")):
            if s.startswith(open_ch) and s.endswith(close_ch) and len(s) > 2:
                bracketed = True
                s = s[1:-1].strip()
                changed = True
                break
        if changed:
            continue

        upper = s.upper()
        for suffix, sign in _suffix_candidates():
            # The character before the marker must belong to a number, so that
            # "ADDR" and "CONCERT" are not read as sign notation.
            if (
                upper.endswith(suffix)
                and len(s) > len(suffix)
                and s[-len(suffix) - 1] in _ADJACENT_TO_NUMBER
            ):
                explicit = sign
                s = s[: -len(suffix)].strip()
                changed = True
                break
        if changed:
            continue

        for prefix, sign in _suffix_candidates():
            if (
                upper.startswith(prefix)
                and len(s) > len(prefix)
                and s[len(prefix)] in "0123456789(.-"
            ):
                explicit = sign
                s = s[len(prefix) :].strip()
                changed = True
                break
        if changed:
            continue

        if s.startswith("-"):
            signed = True
            s = s[1:].strip()
            changed = True
        elif s.startswith("+"):
            s = s[1:].strip()
            changed = True
        elif s.endswith("-"):  # SAP and several core banking exports trail it
            signed = True
            s = s[:-1].strip()
            changed = True

    negative = (explicit == -1) if explicit is not None else (bracketed or signed)
    return s, negative


def _suffix_candidates() -> list[tuple[str, int]]:
    """Sign markers, longest first so CREDIT is matched before CR."""
    pairs = [(m, -1) for m in _SIGN_SUFFIXES_NEGATIVE] + [
        (m, 1) for m in _SIGN_SUFFIXES_POSITIVE
    ]
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def _strip_currency(text: str) -> str:
    """Remove currency symbols and ISO codes, leaving the numeric part."""
    s = "".join(ch for ch in text if ch not in _CURRENCY_SYMBOLS)
    s = s.replace("%", "").strip()

    # A leading or trailing alphabetic token that is a known currency code.
    for _ in range(2):  # a value may be wrapped on both sides: "USD 12 USD"
        match = re.match(r"^([A-Za-z]{1,4})[\s.]*(?=[\d(.,-])", s)
        if match and match.group(1).upper() in _CURRENCY_CODES:
            s = s[match.end() :].strip()
            continue
        match = re.search(r"(?<=[\d).,])[\s.]*([A-Za-z]{1,4})$", s)
        if match and match.group(1).upper() in _CURRENCY_CODES:
            s = s[: match.start()].strip()
            continue
        break
    return s


def _is_thousands_grouping(groups: list[str]) -> bool:
    """Do these separator-delimited groups form a valid thousands grouping?

    ``1|234|567`` yes, ``1|2|3|4`` no. Every group after the first must be
    exactly three digits and the first must be one to three. Without this test,
    OCR noise collapses into a confident, entirely fictional number.
    """
    if len(groups) < 2:
        return False
    if not (1 <= len(groups[0]) <= 3) or not groups[0].isdigit():
        return False
    return all(len(g) == 3 and g.isdigit() for g in groups[1:])


def _digits_to_float(s: str, decimal_sep: str | None) -> float | None:
    """Turn a string of digits and separators into a float.

    ``decimal_sep`` comes from :func:`infer_decimal_separator` when the caller
    knows the document's locale. Without it, fall back to per-value heuristics.
    """
    if not s or not any(ch.isdigit() for ch in s):
        return None

    if decimal_sep == ".":
        s = s.replace(",", "")
    elif decimal_sep == ",":
        s = s.replace(".", "").replace(",", ".")
    else:
        last_dot, last_comma = s.rfind("."), s.rfind(",")
        if last_dot >= 0 and last_comma >= 0:
            # Both present: whichever comes last is the decimal point.
            if last_comma > last_dot:
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif last_comma >= 0:
            groups = s.split(",")
            if _is_thousands_grouping(groups):
                s = s.replace(",", "")
            elif len(groups) == 2:
                s = s.replace(",", ".")  # 1,23 — a decimal comma
            else:
                return None  # 1,23,4 is not a number in any convention
        elif last_dot >= 0:
            groups = s.split(".")
            if len(groups) > 2:
                # 1.234.567 is grouping; 1.2.3.4 is OCR noise. The difference
                # is whether every group after the first is exactly 3 digits.
                if not _is_thousands_grouping(groups):
                    return None
                s = s.replace(".", "")
            # A single dot stays a decimal point — by far the common case.

    if s.count(".") > 1:  # unsalvageable
        return None

    try:
        return float(s)
    except ValueError:
        return None


def parse_amount(value: object, decimal_sep: str | None = None) -> float | None:
    """Parse one cell into a signed float, or ``None`` if it is not a number.

    Handles, in roughly the order they cause trouble in the wild:

    ``(1,234.56)`` accounting negatives, ``1.234,56`` continental separators,
    ``1 234,56`` space grouping, ``$``/``€``/``TND`` symbols and ISO codes,
    ``1,234.56 CR`` and ``1,234.56-`` sign suffixes, unicode minus signs, and
    the nil markers that mean "nothing" rather than "zero".

    >>> parse_amount("(1,234.56)")
    -1234.56
    >>> parse_amount("1.234,56")
    1234.56
    >>> parse_amount("Page 1 of 12") is None
    True
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = unicodedata.normalize("NFKC", str(value))
    text = _clean_spaces(text)
    if not text or is_blank_marker(text):
        return None

    body, negative = _strip_sign_markers(text)

    # Anything alphabetic still standing means this was never an amount.
    # This is the guard that keeps "Page 1 of 12" from becoming 112.
    if re.search(r"[^\d.,]", body):
        return None

    magnitude = _digits_to_float(body, decimal_sep)
    if magnitude is None:
        return None
    return -magnitude if negative else magnitude


def infer_decimal_separator(values: object) -> str | None:
    """Decide the document's decimal separator by looking at every amount.

    Returns ``"."``, ``","`` or ``None`` when there is no evidence either way
    (in which case per-value heuristics apply and are usually right).

    Weighting reflects how conclusive each pattern is. ``1.234,56`` proves the
    comma is decimal. ``1,23`` strongly suggests it. ``1,234`` barely suggests
    the opposite, because it is equally at home in both conventions.
    """
    votes: Counter[str] = Counter()

    for raw in values:
        if raw is None:
            continue
        text = _clean_spaces(unicodedata.normalize("NFKC", str(raw)))
        body, _ = _strip_sign_markers(text)
        if re.search(r"[^\d.,]", body) or not any(c.isdigit() for c in body):
            continue

        dots, commas = body.count("."), body.count(",")
        if dots and commas:
            # Conclusive: the rightmost separator is the decimal point.
            votes["," if body.rfind(",") > body.rfind(".") else "."] += 3.0
        elif commas > 1:
            votes["."] += 2.0  # commas are grouping, so the dot is decimal
        elif dots > 1:
            votes[","] += 2.0
        elif commas == 1:
            after = len(body) - body.rfind(",") - 1
            if after == 2:
                votes[","] += 1.0
            elif after == 3:
                votes["."] += 0.25
        elif dots == 1:
            after = len(body) - body.rfind(".") - 1
            if after == 2:
                votes["."] += 1.0
            elif after == 3:
                votes[","] += 0.25

    if not votes:
        return None
    best, best_score = votes.most_common(1)[0]
    other = votes.get("," if best == "." else ".", 0.0)
    # Refuse to guess when the document genuinely looks mixed.
    if best_score < 1.0 or best_score < other * 1.5:
        return None
    return best


# --------------------------------------------------------------------------- #
# OCR digit repair
# --------------------------------------------------------------------------- #

# Applied only inside cells already judged numeric. Never to text columns:
# running this over a description column turns "Solar" into "501ar".
_OCR_DIGIT_FIXES = {
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "l": "1", "I": "1", "|": "1", "i": "1", "!": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "b": "6", "G": "6",
    "T": "7", "?": "7",
    "B": "8",
    "g": "9", "q": "9",
}


def repair_ocr_digits(value: str) -> str:
    """Fix the letter/digit confusions OCR makes inside numeric cells.

    Only substitutes where the surrounding characters are already digits or
    separators, so an isolated stray letter is left alone rather than being
    forced into a number that then looks trustworthy.
    """
    if not value:
        return value

    chars = list(value)
    n = len(chars)

    def _is_numeric_context(index: int) -> bool:
        """True when a digit sits within two positions on either side."""
        for offset in (-2, -1, 1, 2):
            j = index + offset
            if 0 <= j < n and chars[j].isdigit():
                return True
        return False

    for i, ch in enumerate(chars):
        if ch in _OCR_DIGIT_FIXES and _is_numeric_context(i):
            chars[i] = _OCR_DIGIT_FIXES[ch]

    return "".join(chars)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

_DATE_SPLIT = re.compile(r"[/\-.\s]+")

_MONTH_NAMES = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january", "janv", "janvier"),
            ("feb", "february", "fev", "fév", "fevr", "février"),
            ("mar", "march", "mars"),
            ("apr", "april", "avr", "avril"),
            ("may", "mai"),
            ("jun", "june", "juin"),
            ("jul", "july", "juil", "juillet"),
            ("aug", "august", "aou", "août", "aout"),
            ("sep", "sept", "september", "septembre"),
            ("oct", "october", "octobre"),
            ("nov", "november", "novembre"),
            ("dec", "december", "déc", "decembre", "décembre"),
        ],
        start=1,
    )
    for name in names
}


def _normalise_year(year: int) -> int:
    """Expand a two-digit year. 70-99 → 1900s, 00-69 → 2000s."""
    if year >= 100:
        return year
    return 1900 + year if year >= 70 else 2000 + year


def parse_date(value: object, dayfirst: bool | None = None) -> dt.date | None:
    """Parse a date cell into a :class:`datetime.date`.

    ``03/04/2025`` is 3 April in most of the world and 4 March in the United
    States. Pass ``dayfirst`` from :func:`infer_dayfirst`, which resolves it
    once for the whole document rather than guessing per cell.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value

    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text or is_blank_marker(text):
        return None

    # ISO first — unambiguous, so never subject to the dayfirst question.
    iso = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    parts = [p for p in _DATE_SPLIT.split(text) if p]
    if len(parts) < 3:
        return None
    parts = parts[:3]

    # A named month removes the ambiguity entirely.
    month_from_name: int | None = None
    month_index: int | None = None
    for index, part in enumerate(parts):
        key = part.casefold().rstrip(".")
        if key in _MONTH_NAMES:
            month_from_name = _MONTH_NAMES[key]
            month_index = index
            break

    if month_from_name is not None:
        numbers = [int(p) for i, p in enumerate(parts) if i != month_index and p.isdigit()]
        if len(numbers) != 2:
            return None
        day, year = (numbers[0], numbers[1]) if numbers[1] > 31 else (numbers[1], numbers[0])
        return _safe_date(_normalise_year(year), month_from_name, day)

    if not all(p.isdigit() for p in parts):
        return None
    a, b, c = (int(p) for p in parts)

    # Self-evident cases beat any locale setting.
    if a > 31:
        return _safe_date(_normalise_year(a), b, c)
    if a > 12 and b <= 12:
        return _safe_date(_normalise_year(c), b, a)
    if b > 12 and a <= 12:
        return _safe_date(_normalise_year(c), a, b)

    if dayfirst is None:
        dayfirst = True  # the majority convention outside the United States
    day, month = (a, b) if dayfirst else (b, a)
    return _safe_date(_normalise_year(c), month, day)


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def infer_dayfirst(values: object) -> bool | None:
    """Decide day-first vs month-first from the whole document.

    A single value above 12 in the first position proves day-first. Absent
    that proof we return ``None`` and let the caller keep its default, rather
    than inventing a convention from a handful of ambiguous dates.
    """
    day_first_proof = 0
    month_first_proof = 0

    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if re.match(r"^\d{4}[-/.]", text):
            continue  # ISO, carries no evidence
        parts = [p for p in _DATE_SPLIT.split(text) if p]
        if len(parts) < 3 or not all(p.isdigit() for p in parts[:2]):
            continue
        a, b = int(parts[0]), int(parts[1])
        if a > 12 and b <= 12:
            day_first_proof += 1
        elif b > 12 and a <= 12:
            month_first_proof += 1

    if day_first_proof == month_first_proof:
        return None
    return day_first_proof > month_first_proof
