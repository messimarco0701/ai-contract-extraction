"""
The validation rules are the part of the pipeline that decides what gets
loaded without a human looking at it, so they get the most tests.

The main one works like a known answer test: the generator planted one
problem in ten documents and wrote down which. Running the rules on the ground
truth must flag exactly those problems, with nothing missed and nothing extra.
"""

from datetime import date

import pytest

from contract_extraction.evaluate import load_truth
from contract_extraction.pipeline import ROOT, load_config, validation_rules
from contract_extraction.schema import ContractFields
from contract_extraction.text import read_text
from contract_extraction.validation import normalize, tax_code_ok, validate, vat_checksum_ok

RULES = validation_rules(load_config())


# Check digits worked out by hand, independently of the code:
# 0123456789 -> odd positions 0+2+4+6+8 = 20, even positions doubled
# 2, 6, 10-9=1, 14-9=5, 18-9=9 -> 23. Total 43, check digit 10-3 = 7.
@pytest.mark.parametrize("vat, expected", [
    ("01234567897", True),
    ("12345678903", True),
    ("12345678901", False),   # wrong check digit
    ("00000000000", False),   # passes the arithmetic, never a real number
    ("1234567890", False),    # ten digits
    ("IT01234567897", False), # prefix must be removed before checking
])
def test_vat_checksum(vat, expected):
    assert vat_checksum_ok(vat) is expected


@pytest.mark.parametrize("code, expected", [
    ("01234567897", True),         # company: same rules as the VAT number
    ("0123456789", False),         # company code with a digit missing
    ("RSSMRA85T10A562S", True),    # person, standard format
    ("RSSMRA85T10A56NS", True),    # person with omocodia (digit replaced by letter)
    ("RSSMRA85Z10A562S", False),   # Z is not a month letter
])
def test_tax_code_format(code, expected):
    assert tax_code_ok(code) is expected


def test_planted_problems_are_found_exactly():
    for doc_id, doc in load_truth().items():
        text = read_text(ROOT / "samples" / doc["file"])
        assert validate(doc["record"], RULES, text) == doc["expected_issues"], doc_id


def test_ground_truth_has_clean_and_problem_documents():
    planted = [d for d in load_truth().values() if d["expected_issues"]]
    assert len(planted) == 10


def clean_record(**overrides) -> ContractFields:
    base = dict(company_name="Rossi Logistica S.r.l.", vat_number="01234567897", street="Via Roma 1",
                zip_code="20121", city="Milano", province="MI", contact_email="anna.rossi@rossi.it",
                start_date=date(2026, 3, 1), duration_months=24, end_date=date(2028, 2, 29),
                annual_fee=12000.0, setup_fee=1000.0, employees=80, signed=True,
                signature_date=date(2026, 2, 10))
    return ContractFields(**(base | overrides))


def test_clean_record_passes():
    assert validate(clean_record(), RULES) == []


@pytest.mark.parametrize("overrides, issue", [
    ({"vat_number": "01234567890"}, "vat_checksum"),
    ({"vat_number": "0123456789"}, "vat_format"),
    ({"vat_number": "07654321012"}, "vat_is_own_company"),
    ({"province": "XX"}, "invalid_province"),
    ({"zip_code": "2012"}, "zip_format"),
    ({"contact_email": "anna.rossi@rossi"}, "email_format"),
    ({"end_date": date(2027, 2, 28)}, "end_date_mismatch"),
    ({"duration_months": 7, "end_date": None}, "unusual_duration"),
    ({"annual_fee": 1_200_000.0}, "annual_fee_out_of_range"),
    ({"setup_fee": -50.0}, "setup_fee_out_of_range"),
    ({"setup_fee": 15000.0}, "setup_fee_out_of_range"),
    ({"employees": 0}, "employees_out_of_range"),
    ({"signed": False}, "not_signed"),
    ({"signature_date": date(2026, 3, 20)}, "signature_after_start"),
    ({"signature_date": None}, "signed_without_date"),
    ({"annual_fee": None}, "missing:annual_fee"),
])
def test_each_rule(overrides, issue):
    assert validate(clean_record(**overrides), RULES) == [issue]


def test_end_date_accepts_the_day_after_convention():
    # Some contracts end on 1 March instead of 28/29 February: one day of tolerance
    assert validate(clean_record(end_date=date(2028, 3, 1)), RULES) == []


def test_value_not_in_document_is_flagged():
    text = "Rossi Logistica S.r.l., P.IVA IT 01234 567897, Via Roma 1, 20121 Milano, anna.rossi@rossi.it"
    assert validate(clean_record(), RULES, text) == []
    invented = clean_record(vat_number="12345678903")
    assert "not_in_source:vat_number" in validate(invented, RULES, text)


def test_normalize_cleans_formatting_only():
    raw = ContractFields(vat_number="IT 01234 567897", province=" mi ", tax_code="rssmra 85t10a562s",
                         contact_email="Anna.Rossi@Rossi.IT", company_name="  Rossi   Logistica S.r.l. ")
    clean = normalize(raw)
    assert clean.vat_number == "01234567897"
    assert clean.province == "MI"
    assert clean.tax_code == "RSSMRA85T10A562S"
    assert clean.contact_email == "anna.rossi@rossi.it"
    assert clean.company_name == "Rossi Logistica S.r.l."
