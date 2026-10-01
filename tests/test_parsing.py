from datetime import date

import pytest

from contract_extraction.parsing import add_months, normalize_vat, parse_amount, parse_date


@pytest.mark.parametrize("text, expected", [
    ("decorre dal 1 marzo 2026", date(2026, 3, 1)),
    ("decorre dal 1° marzo 2026", date(2026, 3, 1)),
    ("01/03/2026", date(2026, 3, 1)),
    ("1-3-2026", date(2026, 3, 1)),
    ("01.03.2026", date(2026, 3, 1)),
    ("2026-03-01", date(2026, 3, 1)),
    ("15 Settembre 2026", date(2026, 9, 15)),
    ("31/02/2026", None),        # not a real date
    ("nessuna data", None),
    (None, None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("€ 12.500,00", 12500.0),
    ("12500 EUR", 12500.0),
    ("Euro 12.500", 12500.0),
    ("12.500,50 €", 12500.5),
    ("EUR 1.250.000,00", 1250000.0),
    ("950", 950.0),
    ("12500.0", 12500.0),        # how a spreadsheet cell comes out
    (27750, 27750.0),
    ("da definire", None),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("raw", ["IT01234567897", "IT 01234567897", "01234 567897", "01234567897", "it01234567897"])
def test_normalize_vat(raw):
    assert normalize_vat(raw) == "01234567897"


@pytest.mark.parametrize("start, months, expected", [
    (date(2026, 3, 1), 24, date(2028, 3, 1)),
    (date(2026, 1, 31), 1, date(2026, 2, 28)),
    (date(2027, 8, 31), 6, date(2028, 2, 29)),
    (date(2026, 11, 15), 3, date(2027, 2, 15)),
])
def test_add_months(start, months, expected):
    assert add_months(start, months) == expected
