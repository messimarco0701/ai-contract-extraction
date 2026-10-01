"""
Deterministic checks on an extracted record.

This is the part of the pipeline that decides what can be trusted. The
extractor, whether it is Claude or a regex, only reads. Every rule here is
plain Python with a fixed answer: a partita IVA either passes the checksum or
it doesn't, an end date either matches start plus duration or it doesn't.

A record with no issues is "ok" and can be loaded. Anything else is
"needs_review" and goes to a person, with the reasons listed so they know
where to look. There is no confidence score that lets a doubtful record through.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .parsing import add_months, normalize_vat
from .schema import ContractFields

EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
# Codice fiscale of a person: 6 letters, 2 digits, month letter, 2 digits, 1 letter, 3 digits, check letter.
# Digits can be replaced by letters (omocodia), so those positions accept both.
CF_PERSON = re.compile(r"^[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]$")


def vat_checksum_ok(vat: str | None) -> bool:
    """
    Check digit of the Italian partita IVA (11 digits).

    Digits in odd positions (1st, 3rd, ... 9th) are summed as they are. Digits
    in even positions (2nd, ... 10th) are doubled, and 9 is subtracted when the
    result is above 9. The 11th digit must bring the total to a multiple of 10.
    """
    if not vat or not re.fullmatch(r"\d{11}", vat) or vat == "0" * 11:
        return False
    digits = [int(c) for c in vat]
    total = 0
    for i, d in enumerate(digits[:10]):
        if i % 2 == 0:
            total += d
        else:
            doubled = d * 2
            total += doubled - 9 if doubled > 9 else doubled
    return (10 - total % 10) % 10 == digits[10]


def vat_check_digit(first_ten: str) -> str:
    """The 11th digit that makes a partita IVA valid. Used by the generator."""
    for d in "0123456789":
        if vat_checksum_ok(first_ten + d):
            return d
    raise ValueError(first_ten)


def tax_code_ok(code: str | None) -> bool:
    """A company tax code is 11 digits (same rules as the VAT number), a person's is 16 characters."""
    if not code:
        return False
    if code.isdigit():
        return vat_checksum_ok(code)
    return bool(CF_PERSON.fullmatch(code))


def normalize(record: ContractFields) -> ContractFields:
    """Same value, same spelling: strip spaces and IT prefix from VAT, uppercase codes."""
    data = record.model_dump()
    for key, value in data.items():
        if isinstance(value, str):
            value = " ".join(value.split())
            data[key] = value or None
    data["vat_number"] = normalize_vat(data["vat_number"])
    if data["tax_code"]:
        data["tax_code"] = re.sub(r"\s", "", data["tax_code"]).upper()
    if data["province"]:
        data["province"] = data["province"].upper().strip("() ")
    if data["contact_email"]:
        data["contact_email"] = data["contact_email"].lower()
    return ContractFields(**data)


def _squash(text: str) -> str:
    return re.sub(r"[^0-9a-z@.]", "", text.lower())


def grounding_issues(record: ContractFields, source_text: str) -> list[str]:
    """
    Identifiers must literally appear in the document.

    A model can produce a perfectly formatted VAT number that is not in the
    contract at all. Checking that the digits are really there (ignoring spaces
    and punctuation) catches that, and it costs nothing.
    """
    issues = []
    squashed = _squash(source_text)
    digits = re.sub(r"\D", "", source_text)
    for field in ("vat_number", "zip_code"):
        value = getattr(record, field)
        if value and value not in digits:
            issues.append(f"not_in_source:{field}")
    for field in ("tax_code", "contact_email"):
        value = getattr(record, field)
        if value and _squash(value) not in squashed:
            issues.append(f"not_in_source:{field}")
    return issues


def validate(record: ContractFields, rules: dict, source_text: str | None = None) -> list[str]:
    """All the reasons a record can't be loaded as it is. Empty list means ok."""
    r = record
    issues = [f"missing:{f}" for f in rules["mandatory_fields"] if getattr(r, f) is None]

    if r.vat_number:
        if not re.fullmatch(r"\d{11}", r.vat_number):
            issues.append("vat_format")
        elif not vat_checksum_ok(r.vat_number):
            issues.append("vat_checksum")
        if r.vat_number == rules.get("own_vat_number"):
            issues.append("vat_is_own_company")
    if r.tax_code and not tax_code_ok(r.tax_code):
        issues.append("tax_code_format")
    if r.zip_code and not re.fullmatch(r"\d{5}", r.zip_code):
        issues.append("zip_format")
    if r.province and r.province not in rules["provinces"]:
        issues.append("invalid_province")
    if r.contact_email and not EMAIL.fullmatch(r.contact_email):
        issues.append("email_format")

    if r.duration_months is not None and r.duration_months not in rules["duration_months_allowed"]:
        issues.append("unusual_duration")
    if r.start_date and r.duration_months and r.end_date:
        expected = add_months(r.start_date, r.duration_months) - timedelta(days=1)
        if abs((r.end_date - expected).days) > rules["end_date_tolerance_days"]:
            issues.append("end_date_mismatch")

    if r.annual_fee is not None and not rules["annual_fee_min"] <= r.annual_fee <= rules["annual_fee_max"]:
        issues.append("annual_fee_out_of_range")
    if r.setup_fee is not None:
        cap = (r.annual_fee or 0) * rules["setup_fee_max_share_of_annual"]
        if r.setup_fee < 0 or (r.annual_fee and r.setup_fee > cap):
            issues.append("setup_fee_out_of_range")
    if r.employees is not None and not rules["employees_min"] <= r.employees <= rules["employees_max"]:
        issues.append("employees_out_of_range")

    if r.signed is False:
        issues.append("not_signed")
    # Added after the first baseline run approved an unsigned contract: the
    # supplier's "Firmato digitalmente" had been read as the customer's signature.
    # A real signature always comes with a date, so signed without a date is suspect.
    if r.signed and r.signature_date is None:
        issues.append("signed_without_date")
    if r.signature_date and r.start_date and r.signature_date > r.start_date:
        issues.append("signature_after_start")

    if source_text is not None:
        issues += grounding_issues(r, source_text)
    return issues
