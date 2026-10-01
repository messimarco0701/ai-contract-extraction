"""
Two ways of reading a contract, behind the same interface.

ClaudeExtractor sends the document text to the Anthropic API and gets back a
ContractFields object through structured outputs, so the response is
validated against the schema before it reaches the rest of the pipeline.

RuleBasedExtractor is the baseline: labels, regular expressions and a few
heuristics, the way this job is usually automated before an LLM is involved.
It exists for two reasons. The repo has to run and its tests have to pass
without an API key, and it gives an honest reference point: the evaluation
shows exactly where rules work (fixed forms) and where they break (free text).

Neither extractor decides whether a value is trustworthy. That is done
afterwards, by the same validation rules, whatever produced the record.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import anthropic

from .parsing import ANY_DATE, digits_only, parse_amount, parse_date
from .schema import ContractFields


@dataclass
class ExtractionResult:
    fields: ContractFields
    error: str | None = None


class Extractor(ABC):
    name: str

    @abstractmethod
    def extract(self, text: str) -> ExtractionResult:
        """Read the document text and return the fields found. Missing fields stay None."""


# ---------------------------------------------------------------- Claude

SYSTEM_PROMPT = """You extract customer data from Italian B2B service contracts and activation forms. The data is loaded into a CRM, so a wrong value is worse than an empty one.

The document is inside <document> tags. It can be a contract, an order confirmation, an activation form or a spreadsheet export, and the layout changes from one document to the next. Text from scanned or exported files can have odd spacing or line breaks.

The supplier is {own_company} (VAT {own_vat}). Every field refers to the customer, never to the supplier.

How to fill the fields:
- Copy what the document says. Do not compute or infer values that are not written: if the end date is not stated, return null even if you could calculate it from start date and duration.
- If a field is missing, unreadable or ambiguous, return null. Nulls are reviewed by a person; a guessed value can end up in the CRM without anyone noticing.
- Copy identifiers exactly, even if they look wrong (a VAT number that seems one digit off, an email without a domain suffix). They are validated later and the reviewer needs to see what was written.
- vat_number: digits only, without the IT prefix and without spaces.
- Dates: YYYY-MM-DD. Amounts: plain numbers in euro, VAT excluded (12.500,00 becomes 12500.0).
- duration_months: convert years to months (2 anni = 24).
- setup_fee: 0 if the document says there is no setup or activation cost, null if it doesn't mention one.
- payment_terms_days: days from the invoice date; 0 for rimessa diretta, a vista or payment at invoice date.
- billing_frequency: ogni tre mesi is trimestrale, una volta l'anno is annuale, and so on.
- signed: true only if the customer's signature is there (a signature, "firmato", "Sì", "[firma]"). false if the customer signature space is visibly empty. null if the document has no signature section."""


class ClaudeExtractor(Extractor):
    name = "claude"

    def __init__(self, config: dict, client: anthropic.Anthropic | None = None):
        self.model = config["extraction"]["model"]
        self.max_tokens = config["extraction"]["max_tokens"]
        self.effort = config["extraction"].get("effort")
        own = config["own_company"]
        self.system = SYSTEM_PROMPT.format(own_company=own["name"], own_vat=own["vat_number"])
        # The client reads ANTHROPIC_API_KEY from the environment. Tests pass a fake one.
        self.client = client or anthropic.Anthropic()

    def extract(self, text: str) -> ExtractionResult:
        kwargs = {}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                messages=[{"role": "user", "content": f"<document>\n{text}\n</document>"}],
                output_format=ContractFields,
                **kwargs,
            )
        except anthropic.APIConnectionError as exc:
            return ExtractionResult(ContractFields(), f"extraction_failed: connection error ({exc})")
        except anthropic.APIStatusError as exc:
            return ExtractionResult(ContractFields(), f"extraction_failed: API error {exc.status_code}")

        if response.stop_reason == "refusal":
            return ExtractionResult(ContractFields(), "extraction_failed: model declined the request")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            return ExtractionResult(ContractFields(), f"extraction_failed: no structured output ({response.stop_reason})")
        return ExtractionResult(response.parsed_output)


# ---------------------------------------------------------------- rule-based baseline

LEGAL_FORM = r"(?:S\.?r\.?l\.?s?\.?|S\.?p\.?A\.?|S\.?a\.?s\.?|S\.?n\.?c\.?)"
COMPANY = re.compile(rf"([A-Z][\w'&]*(?:\s+[\w'&]+){{0,4}}\s+{LEGAL_FORM})")
STREET = re.compile(r"\b((?:Via|Viale|Corso|Piazza|Piazzale|Largo|Vicolo)\s+[^,\n|]+?\s+\d+[A-Za-z]?)\b", re.IGNORECASE)
ZIP_CITY_PROV = re.compile(r"\b(\d{5})\s+([A-Za-zÀ-ÿ' ]+?)\s+\(?\s*([A-Z]{2})\s*\)?(?=\s|$|,|\.)")
EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*")
VAT_NEAR_LABEL = re.compile(r"(?:P\.?\s?IVA|Partita IVA|VAT)[^0-9]{0,20}?((?:IT)?\s?\d[\d ]{9,13}\d)", re.IGNORECASE)

BILLING_KEYWORDS = ["mensile", "bimestrale", "trimestrale", "semestrale", "annuale"]


def label_value(lines: list[str], labels: list[str]) -> str | None:
    """
    Value written next to a label at the start of a line: 'CAP 20121', 'CAP: 20121',
    'CAP | 20121', 'CAP ...... 20121'. Returns None if the label is there but empty.
    """
    for label in labels:
        pattern = re.compile(rf"^\s*{re.escape(label)}(?=[\s:|.]|$)[\s:|.]*(.*)$", re.IGNORECASE)
        for line in lines:
            m = pattern.match(line)
            if m:
                value = m.group(1).strip(" |")
                return value or None
    return None


class RuleBasedExtractor(Extractor):
    """Labels first, then patterns in free text. No knowledge of the specific templates."""

    name = "rules"

    def __init__(self, config: dict):
        own = config["own_company"]
        self.own_name = own["name"].lower()
        self.own_vat = own["vat_number"]
        self.own_domain = own["email_domain"]
        self.own_zip = own["zip_code"]
        self.own_street = own["street"].lower()

    def extract(self, text: str) -> ExtractionResult:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        flat = " ".join(text.split())
        get = lambda *labels: label_value(lines, list(labels))  # noqa: E731

        f: dict = {}
        f["company_name"] = self._company(lines, flat, get)
        f["vat_number"] = self._vat(flat, get)
        tax = get("Codice fiscale", "C.F.", "C . F.")
        m = re.search(r"\b([A-Z0-9]{16}|\d{11})\b", re.sub(r"(?<=\d) (?=\d)", "", tax.upper())) if tax else None
        f["tax_code"] = m.group(1) if m else None

        street = get("Indirizzo", "Via e civico", "Sede legale", "Sede")
        m = STREET.search(street) if street else None
        if m:
            street = m.group(1)
        else:
            street = next((m.group(1) for m in STREET.finditer(flat) if m.group(1).lower() != self.own_street), None)
        f["street"] = street

        f["zip_code"], f["city"], f["province"] = get("CAP"), get("Comune", "Città"), get("Provincia", "Prov")
        if not f["zip_code"]:
            for m in ZIP_CITY_PROV.finditer(flat):
                if m.group(1) != self.own_zip:
                    f["zip_code"], f["city"], f["province"] = m.group(1), m.group(2).strip(), m.group(3)
                    break

        f["contact_name"] = get("Nome e cognome", "Referente", "Contatto")
        if not f["contact_name"]:
            m = re.search(r"(?:referente operativo è|Gentile)\s+([A-Z][a-zà-ù']+\s+[A-Z][a-zà-ù']+)", flat)
            f["contact_name"] = m.group(1) if m else None
        f["contact_email"] = next((e.rstrip(".") for e in EMAIL.findall(flat) if self.own_domain not in e), None)

        f["start_date"] = parse_date(get("Data attivazione", "Data inizio", "Decorrenza")) or self._date_after(
            flat, r"(?:decorre dal|a partire dal)")
        f["end_date"] = parse_date(get("Scadenza", "Data fine", "Termine")) or self._date_after(
            flat, r"(?:con scadenza il|fino al)")
        f["duration_months"] = self._duration(get("Durata contratto", "Durata (mesi)", "Durata mesi", "Durata"), flat)
        f["annual_fee"] = parse_amount(get("Canone annuale", "Canone annuo", "Canone/anno")) or self._amount_after(
            flat, r"canone annuo di")
        f["setup_fee"] = self._setup(get("Una tantum attivazione", "Fee di attivazione", "Setup (EUR)", "Attivazione"), flat)
        emp = get("Numero dipendenti", "Dipendenti", "N. dip.")
        m = re.search(r"(\d+)\s+dipendenti", flat)
        f["employees"] = int(digits_only(emp)) if emp and digits_only(emp) else (int(m.group(1)) if m else None)
        f["payment_terms_days"] = self._payment(get("Termini di pagamento", "Pagamento (gg)", "Pagamento", "Pagam."), flat)
        f["billing_frequency"] = self._billing(get("Fatturazione", "Fatturaz."), flat)
        f["signed"], f["signature_date"] = self._signature(get, flat)
        return ExtractionResult(ContractFields(**f))

    # Each helper handles one field. They are deliberately simple: this is a baseline.

    def _company(self, lines, flat, get):
        labelled = get("Ragione sociale", "Rag. sociale", "Denominazione", "Cliente (ragione sociale)")
        if labelled:
            return labelled
        m = re.search(rf"Spett\.le\s+(.+?{LEGAL_FORM})", flat)
        if m:
            return m.group(1)
        return next((c for c in COMPANY.findall(flat) if self.own_name not in c.lower()), None)

    def _vat(self, flat, get):
        candidates = [get("Partita IVA", "P. IVA", "VAT / P.IVA", "P.IVA")] + VAT_NEAR_LABEL.findall(flat)
        for c in candidates:
            digits = digits_only(c)[:11] if c else ""
            if len(digits) == 11 and digits != self.own_vat:
                return digits
        return None

    @staticmethod
    def _date_after(flat, prefix):
        m = re.search(rf"{prefix}\s+({ANY_DATE.pattern})", flat, re.IGNORECASE)
        return parse_date(m.group(1)) if m else None

    @staticmethod
    def _amount_after(flat, prefix):
        m = re.search(rf"{prefix}\s+((?:€|EUR|Euro)?\s*[\d.,]+\s*(?:€|EUR)?)", flat, re.IGNORECASE)
        return parse_amount(m.group(1)) if m else None

    @staticmethod
    def _duration(value, flat):
        source = value or (m.group(0) if (m := re.search(r"durata di\s+\d+\s+(?:mesi|anni)", flat)) else "")
        m = re.search(r"(\d+)\s*(mesi|anni)?", source)
        if not m:
            return None
        n = int(m.group(1))
        return n * 12 if m.group(2) == "anni" else n

    def _setup(self, value, flat):
        if value:
            return parse_amount(value)
        if re.search(r"non sono previsti costi di attivazione", flat, re.IGNORECASE):
            return 0.0
        return self._amount_after(flat, r"(?:una tantum di|costo di avvio di)")

    @staticmethod
    def _payment(value, flat):
        source = value or flat
        if re.search(r"rimessa diretta|a vista", source, re.IGNORECASE):
            return 0
        if value and value.strip().isdigit():
            return int(value)
        m = re.search(r"(\d+)\s*(?:gg|giorni)", source, re.IGNORECASE)
        return int(m.group(1)) if m else None

    @staticmethod
    def _billing(value, flat):
        source = (value or "").lower()
        if not source:
            m = re.search(r"fattur\w*[^.]{0,40}", flat, re.IGNORECASE)
            source = m.group(0).lower() if m else ""
        return next((k for k in BILLING_KEYWORDS if k in source), None)

    @staticmethod
    def _signature(get, flat):
        value = get("Firma cliente", "Firmato", "Sottoscrizione", "Timbro e firma")
        date_value = parse_date(get("Data firma", "Data sottoscrizione"))
        if value:
            signed = not re.match(r"^(no\b|_+$)", value.strip(), re.IGNORECASE)
            return signed, date_value or parse_date(value)
        m = re.search(rf"sottoscritto il\s+({ANY_DATE.pattern})", flat, re.IGNORECASE)
        if m:
            return True, parse_date(m.group(1))
        if re.search(r"Firmato digitalmente da", flat):
            return True, date_value
        if re.search(r"Il Cliente.{0,80}_{5,}|Cliente\s+_{5,}", flat):
            return False, None
        return None, date_value


# ---------------------------------------------------------------- choice

def get_extractor(kind: str, config: dict) -> tuple[Extractor, str | None]:
    """
    Returns the extractor and an optional message for the user.
    'auto' uses Claude when ANTHROPIC_API_KEY is set and the baseline otherwise.
    """
    if kind == "rules":
        return RuleBasedExtractor(config), None
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if kind == "claude":
        if not has_key:
            raise SystemExit("ANTHROPIC_API_KEY is not set. Set it, or run with --extractor rules.")
        return ClaudeExtractor(config), None
    if kind == "auto":
        if has_key:
            return ClaudeExtractor(config), None
        return RuleBasedExtractor(config), "ANTHROPIC_API_KEY not set: using the offline rule-based baseline."
    raise ValueError(f"Unknown extractor: {kind}")
