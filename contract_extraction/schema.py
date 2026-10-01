"""
The target record: what a CRM or ERP needs to set up a new customer.

Every field is optional on purpose. A contract that doesn't state the setup fee
should produce an empty setup fee, not a zero and not a guess. Deciding whether
an empty field is acceptable is the job of the validation layer, not of the
extractor.

The same Pydantic model is used as the structured output schema for Claude, as
the return type of the offline baseline and as the format of the ground truth,
so all three can be compared field by field.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BillingFrequency = Literal["mensile", "bimestrale", "trimestrale", "semestrale", "annuale"]


class ContractFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = Field(None, description="Legal name of the CUSTOMER, including legal form (S.r.l., S.p.A.)")
    vat_number: str | None = Field(None, description="Customer partita IVA: exactly 11 digits, no IT prefix, no spaces")
    tax_code: str | None = Field(None, description="Customer codice fiscale as written, uppercase, no spaces")
    street: str | None = Field(None, description="Registered office street and number, e.g. 'Via Roma 12'")
    zip_code: str | None = Field(None, description="Registered office CAP, 5 digits")
    city: str | None = Field(None, description="Registered office city")
    province: str | None = Field(None, description="Registered office province code, 2 uppercase letters")
    contact_name: str | None = Field(None, description="Customer contact person, first name and last name")
    contact_email: str | None = Field(None, description="Email of the customer contact person, exactly as written")
    start_date: date | None = Field(None, description="Contract start date")
    duration_months: int | None = Field(None, description="Contract duration in months")
    end_date: date | None = Field(None, description="Contract end date, only if written in the document")
    annual_fee: float | None = Field(None, description="Annual fee in euro, excluding VAT")
    setup_fee: float | None = Field(None, description="One-off setup or activation fee in euro, excluding VAT")
    employees: int | None = Field(None, description="Number of employees of the customer covered by the contract")
    payment_terms_days: int | None = Field(None, description="Days from invoice date to payment. 0 for immediate payment")
    billing_frequency: BillingFrequency | None = Field(None, description="How often the customer is invoiced")
    signed: bool | None = Field(None, description="True if the document shows the customer signature, False if the signature block is empty")
    signature_date: date | None = Field(None, description="Date the customer signed")


FIELDS: list[str] = list(ContractFields.model_fields)
