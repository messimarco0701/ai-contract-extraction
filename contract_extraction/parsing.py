"""
Parsers for the way Italian documents write dates, amounts and identifiers.

These are used in three places: by the rule-based baseline to read the text, by
the validation layer to normalize what any extractor returned, and by the
evaluation to compare values written in different ways. Keeping them in one
module means "01/03/2026" and "1 marzo 2026" are the same date everywhere.
"""

from __future__ import annotations

import re
from datetime import date

MONTHS_IT = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
_MONTH_NAMES = "|".join(MONTHS_IT)

# 1 marzo 2026, 1° marzo 2026, 01 Marzo 2026
DATE_WORDS = re.compile(rf"\b(\d{{1,2}})\s*°?\s+({_MONTH_NAMES})\s+(\d{{4}})\b", re.IGNORECASE)
# 01/03/2026, 1-3-2026, 01.03.2026
DATE_DMY = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")
# 2026-03-01
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
ANY_DATE = re.compile(f"{DATE_WORDS.pattern}|{DATE_DMY.pattern}|{DATE_ISO.pattern}", re.IGNORECASE)


def parse_date(text: str | None) -> date | None:
    """First date found in the text, in any of the formats above. None if there is none."""
    if not text:
        return None
    for pattern, order in ((DATE_ISO, "ymd"), (DATE_WORDS, "dMy"), (DATE_DMY, "dmy")):
        m = pattern.search(text)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if order == "ymd":
                return date(int(a), int(b), int(c))
            if order == "dMy":
                return date(int(c), MONTHS_IT[b.lower()], int(a))
            return date(int(c), int(b), int(a))
        except ValueError:
            return None
    return None


AMOUNT = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


def parse_amount(text: str | None) -> float | None:
    """
    Reads '€ 12.500,00', '12500 EUR', 'Euro 12.500', '12.500,5'.

    Italian uses the dot for thousands and the comma for decimals. A plain
    '12.500' is read as twelve thousand five hundred, because a fee written as
    twelve euro and fifty cents with a dot does not happen in these documents.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = AMOUNT.search(text.replace(" ", " "))
    if not m:
        return None
    raw = m.group(1).replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def digits_only(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def normalize_vat(text: str | None) -> str | None:
    """'IT 01234567890', '01234 567890' and 'IT01234567890' all become '01234567890'."""
    if not text:
        return None
    cleaned = re.sub(r"[\s.\-]", "", str(text).upper())
    cleaned = cleaned.removeprefix("IT")
    return cleaned or None


def add_months(start: date, months: int) -> date:
    """Same day N months later, clamped to the end of the month (31 Jan + 1 = 28 Feb)."""
    y, m = divmod(start.month - 1 + months, 12)
    year, month = start.year + y, m + 1
    for day in (start.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError("unreachable")
