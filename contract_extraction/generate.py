"""
Generates the synthetic contracts in samples/ and their ground truth in ground_truth/.

The point is to reproduce the variety that makes contract reading hard to
automate with rules alone: five layouts, fields in a different order in each,
dates written as "1° marzo 2026", "01/03/2026" or "2026-03-01", amounts as
"€ 12.500,00" or "12500 EUR", VAT numbers with and without the IT prefix, the
supplier's own data sitting next to the customer's, optional fields that are
sometimes just not there, and one document that looks like a bad scan.

A few documents carry a problem on purpose (wrong VAT check digit, end date
that doesn't match the duration, missing signature...). The ground truth
records which problem was planted, so the validation layer can be tested
against a known answer.

Everything is driven by a fixed seed: running it twice gives the same documents.

Run it with:  python -m contract_extraction generate
"""

from __future__ import annotations

import json
import random
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from faker import Faker
from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .parsing import add_months
from .schema import ContractFields
from .validation import vat_check_digit

ROOT = Path(__file__).resolve().parents[1]
SEED = 42

OWN_COMPANY = "Esempio Servizi S.r.l."
OWN_VAT = "07654321012"
OWN_ADDRESS = "Via dell'Esempio 1, 20100 Milano (MI)"

CITIES = [
    ("Milano", "20121", "MI"), ("Torino", "10121", "TO"), ("Bologna", "40121", "BO"),
    ("Verona", "37121", "VR"), ("Bergamo", "24121", "BG"), ("Brescia", "25121", "BS"),
    ("Padova", "35121", "PD"), ("Firenze", "50121", "FI"), ("Roma", "00184", "RM"),
    ("Napoli", "80133", "NA"), ("Bari", "70121", "BA"), ("Monza", "20900", "MB"),
    ("Varese", "21100", "VA"), ("Treviso", "31100", "TV"), ("Parma", "43121", "PR"),
    ("Modena", "41121", "MO"), ("Genova", "16121", "GE"), ("Ancona", "60121", "AN"),
    ("Udine", "33100", "UD"), ("Trento", "38122", "TN"), ("Como", "22100", "CO"),
    ("Vicenza", "36100", "VI"), ("Perugia", "06121", "PG"), ("Forlì", "47121", "FC"),
]
STREET_TYPES = ["Via", "Via", "Via", "Viale", "Corso", "Piazza", "Largo"]
STREET_NAMES = ["Giuseppe Garibaldi", "Giuseppe Mazzini", "Camillo Cavour", "Giuseppe Verdi", "Roma",
                "Dante Alighieri", "Alessandro Manzoni", "Guglielmo Marconi", "Alessandro Volta",
                "Giacomo Leopardi", "dell'Industria", "dei Mille", "XX Settembre", "IV Novembre",
                "della Repubblica", "Vittorio Veneto", "dell'Artigianato", "San Francesco"]
SECTORS = ["Logistica", "Meccanica", "Impianti", "Alimentare", "Software", "Costruzioni", "Tessile",
           "Trasporti", "Energia", "Packaging", "Arredamenti", "Elettronica", "Servizi", "Plastica"]
LEGAL_FORMS = ["S.r.l.", "S.r.l.", "S.r.l.", "S.p.A.", "S.r.l.s."]

MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
          "settembre", "ottobre", "novembre", "dicembre"]
BILLING_WORDS = {
    "mensile": ["mensile", "ogni mese"],
    "bimestrale": ["bimestrale", "ogni due mesi"],
    "trimestrale": ["trimestrale", "ogni tre mesi"],
    "semestrale": ["semestrale", "ogni sei mesi"],
    "annuale": ["annuale", "una volta l'anno"],
}
PAYMENT_WORDS = {
    0: ["rimessa diretta", "pagamento a vista fattura"],
    30: ["bonifico bancario a 30 giorni data fattura", "30 gg d.f.", "bonifico a 30 giorni"],
    60: ["bonifico bancario a 60 giorni data fattura", "60 gg d.f.", "bonifico a 60 giorni"],
    90: ["bonifico a 90 giorni data fattura", "90 gg d.f."],
}

# (template, planted problem). The order is fixed so the ground truth is stable.
PLAN = [
    ("contract", None), ("form", None), ("letter", None), ("sheet", None), ("excel_v", None),
    ("contract", "vat_checksum"), ("form", None), ("letter", "missing:vat_number"),
    ("sheet", "end_date_mismatch"), ("excel_h", None), ("contract", "not_signed"),
    ("form", "email_format"), ("letter", None), ("sheet", "invalid_province"),
    ("excel_v", "vat_checksum"), ("contract", "annual_fee_out_of_range"), ("form", "tax_code_format"),
    ("letter", "signature_after_start"), ("sheet", None), ("excel_h", None), ("scan", None),
    ("contract", None), ("form", None), ("letter", None),
]


# ---------------------------------------------------------------- formatting

def it_number(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_amount(x: float, style: str) -> str:
    return {
        "euro_sign": f"€ {it_number(x)}",
        "eur_plain": f"{int(x)} EUR" if x == int(x) else f"{x:.2f} EUR",
        "euro_word": f"Euro {it_number(x)}",
        "suffix": f"{it_number(x)} €",
        "eur_prefix": f"EUR {it_number(x)}",
    }[style]


def fmt_date(d: date, style: str) -> str:
    if style == "words":
        day = "1°" if d.day == 1 else str(d.day)
        return f"{day} {MONTHS[d.month - 1]} {d.year}"
    return {"slash": d.strftime("%d/%m/%Y"), "dot": d.strftime("%d.%m.%Y"), "iso": d.isoformat()}[style]


def fmt_vat(v: str, style: str) -> str:
    return {"it": f"IT{v}", "plain": v, "it_space": f"IT {v}", "spaced": f"{v[:5]} {v[5:]}"}[style]


def ascii_slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c for c in text.lower() if c.isalnum())


# ---------------------------------------------------------------- records

class Builder:
    def __init__(self, seed: int = SEED):
        self.rng = random.Random(seed)
        self.fake = Faker("it_IT")
        self.fake.seed_instance(seed)

    def vat(self) -> str:
        first_ten = "".join(self.rng.choice("0123456789") for _ in range(7)) + f"{self.rng.randint(1, 100):03d}"
        return first_ten + vat_check_digit(first_ten)

    def record(self, planted: str | None) -> dict:
        rng, fake = self.rng, self.fake
        last = fake.last_name()
        legal = rng.choice(LEGAL_FORMS)
        sector = rng.choice(SECTORS)
        company = (f"{last} {sector} {legal}" if rng.random() < 0.7
                   else f"{last} & {fake.last_name()} {legal}")
        city, zip_code, prov = rng.choice(CITIES)
        street = f"{rng.choice(STREET_TYPES)} {rng.choice(STREET_NAMES)} {rng.randint(1, 180)}"
        vat = self.vat()
        tax_code = vat if rng.random() < 0.75 else self.vat()
        first, surname = fake.first_name(), fake.last_name()
        domain = ascii_slug(last + sector if " & " not in company else last) + ".it"
        email = f"{ascii_slug(first)}.{ascii_slug(surname)}@{domain}"

        start = date(2026, rng.randint(1, 12), rng.choice([1, 1, 1, 15]))
        duration = rng.choice([12, 24, 24, 36, 12])
        end = add_months(start, duration) - timedelta(days=1)
        annual = float(rng.randrange(2400, 60000, 50))
        setup = rng.choice([None, 0.0, round(annual * rng.uniform(0.05, 0.3), -1), 1500.0, 950.0])

        rec = dict(
            company_name=company, vat_number=vat,
            tax_code=tax_code if rng.random() < 0.85 else None,
            street=street, zip_code=zip_code, city=city, province=prov,
            contact_name=f"{first} {surname}",
            contact_email=email if rng.random() < 0.92 else None,
            start_date=start, duration_months=duration,
            end_date=end if rng.random() < 0.7 else None,
            annual_fee=annual, setup_fee=setup,
            employees=rng.randint(15, 1200) if rng.random() < 0.8 else None,
            payment_terms_days=rng.choice([30, 30, 60, 0, 90]),
            billing_frequency=rng.choice(list(BILLING_WORDS)),
            signed=True, signature_date=start - timedelta(days=rng.randint(5, 40)),
        )
        legal_rep = f"{fake.first_name()} {fake.last_name()}"

        if planted == "vat_checksum":
            rec["vat_number"] = vat[:10] + str((int(vat[10]) + 1) % 10)  # typo in the VAT only
        elif planted == "missing:vat_number":
            rec["vat_number"] = None
            rec["tax_code"] = None
        elif planted == "end_date_mismatch":
            rec["end_date"] = add_months(start, duration - 12) - timedelta(days=1)
        elif planted == "not_signed":
            rec["signed"], rec["signature_date"] = False, None
        elif planted == "email_format":
            rec["contact_email"] = email.rsplit(".", 1)[0]  # top level domain dropped
        elif planted == "invalid_province":
            rec["province"] = prov + prov[0]  # "MIM": a typo nobody notices in a PDF
        elif planted == "annual_fee_out_of_range":
            rec["annual_fee"] = annual * 10  # one zero too many
        elif planted == "tax_code_format":
            rec["tax_code"] = vat[:10]  # a digit dropped
        elif planted == "signature_after_start":
            rec["signature_date"] = start + timedelta(days=rng.randint(10, 30))
        rec["_legal_rep"] = legal_rep
        rec["_contract_no"] = f"{rng.randint(1000, 9999)}/{start.year}"
        return rec


# ---------------------------------------------------------------- pdf helpers

class Doc(FPDF):
    def __init__(self):
        super().__init__()
        self.core_fonts_encoding = "windows-1252"  # the default latin-1 has no euro sign
        self.creation_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.set_auto_page_break(True, margin=15)
        self.set_margins(18, 15, 18)
        self.add_page()

    def para(self, text: str, size: float = 10, style: str = "", h: float = 5, align: str = "L", font: str = "Helvetica"):
        self.set_font(font, style, size)
        self.multi_cell(0, h, text, align=align, new_x="LMARGIN", new_y="NEXT")

    def gap(self, h: float = 3):
        self.ln(h)

    def kv(self, rows: list[tuple[str, str]], widths=(62, 112), size: float = 9.5):
        self.set_font("Helvetica", "", size)
        with self.table(col_widths=widths, first_row_as_headings=False, line_height=6.5) as table:
            for label, value in rows:
                row = table.row()
                row.cell(label)
                row.cell(value)


def amount_style(rng: random.Random) -> str:
    return rng.choice(["euro_sign", "eur_plain", "euro_word", "suffix", "eur_prefix"])


def opt(value, fmt=str) -> str:
    return "" if value is None else fmt(value)


# ---------------------------------------------------------------- templates

def render_contract(rec: dict, rng: random.Random, path: Path) -> None:
    ds, am, vs = rng.choice(["words", "slash"]), amount_style(rng), rng.choice(["it", "plain", "it_space"])
    d = Doc()
    d.para(f"CONTRATTO DI FORNITURA DI SERVIZI N. {rec['_contract_no']}", 13, "B", 7, "C")
    d.gap(4)
    d.para("TRA", 10, "B")
    d.para(f"{OWN_COMPANY}, con sede legale in {OWN_ADDRESS}, P.IVA IT{OWN_VAT}, in persona dell'amministratore "
           f"delegato, di seguito \"il Fornitore\"", align="J")
    d.para("E", 10, "B")
    ids = f"Partita IVA {fmt_vat(rec['vat_number'], vs)}" if rec["vat_number"] else ""
    if rec["tax_code"]:
        ids += f", Codice Fiscale {rec['tax_code']}"
    d.para(f"{rec['company_name']}, con sede legale in {rec['street']}, {rec['zip_code']} {rec['city']} "
           f"({rec['province']}), {ids}, in persona del legale rappresentante {rec['_legal_rep']}, "
           f"di seguito \"il Cliente\"", align="J")
    d.gap()
    d.para("Art. 1  Oggetto", 10, "B")
    scope = "Il Fornitore mette a disposizione del Cliente la propria piattaforma di servizi"
    if rec["employees"]:
        scope += f", destinata a {rec['employees']} dipendenti del Cliente"
    d.para(scope + ", secondo quanto descritto nell'Allegato A.", align="J")
    d.para("Art. 2  Durata", 10, "B")
    dur = (f"Il presente contratto decorre dal {fmt_date(rec['start_date'], ds)} e ha durata di "
           f"{rec['duration_months']} mesi")
    if rec["end_date"]:
        dur += f", con scadenza il {fmt_date(rec['end_date'], ds)}"
    d.para(dur + ". Alla scadenza si rinnova tacitamente per un anno, salvo disdetta inviata con "
           "almeno 90 giorni di preavviso.", align="J")
    d.para("Art. 3  Corrispettivi", 10, "B")
    fees = f"Il Cliente corrisponde al Fornitore un canone annuo di {fmt_amount(rec['annual_fee'], am)} oltre IVA"
    if rec["setup_fee"] is not None:
        fees += f" e un importo una tantum di {fmt_amount(rec['setup_fee'], am)} per l'attivazione del servizio"
    d.para(fees + ".", align="J")
    d.para("Art. 4  Fatturazione e pagamenti", 10, "B")
    d.para(f"La fatturazione avviene con cadenza {rng.choice(BILLING_WORDS[rec['billing_frequency']][:1])}. "
           f"Modalità di pagamento: {rng.choice(PAYMENT_WORDS[rec['payment_terms_days']])}.", align="J")
    d.para("Art. 5  Referenti", 10, "B")
    ref = f"Per il Cliente il referente operativo è {rec['contact_name']}"
    if rec["contact_email"]:
        ref += f", e-mail {rec['contact_email']}"
    d.para(ref + ". Per il Fornitore: contratti@esempioservizi.it.", align="J")
    d.gap(6)
    place = rec["city"]
    d.para(f"{place}, {fmt_date(rec['signature_date'], ds)}" if rec["signature_date"] else f"{place}, ____________")
    d.gap(4)
    d.para("Il Fornitore                                                    Il Cliente")
    if rec["signed"]:
        d.para(f"Firmato digitalmente                                       Firmato digitalmente da {rec['_legal_rep']}", 9)
    else:
        d.para("Firmato digitalmente                                       ______________________________", 9)
    d.output(str(path))


def render_form(rec: dict, rng: random.Random, path: Path) -> None:
    ds, am, vs = rng.choice(["slash", "dot"]), amount_style(rng), rng.choice(["it", "plain", "spaced"])
    d = Doc()
    d.para("MODULO DI ATTIVAZIONE SERVIZIO", 14, "B", 8)
    d.para(f"Codice pratica {rec['_contract_no']}   |   Fornitore: {OWN_COMPANY}", 8)
    d.gap(4)
    sections = [
        ("DATI AZIENDA", [("Ragione sociale", rec["company_name"]),
                          ("Partita IVA", opt(rec["vat_number"], lambda v: fmt_vat(v, vs))),
                          ("Codice fiscale", opt(rec["tax_code"])),
                          ("Numero dipendenti", opt(rec["employees"]))]),
        ("SEDE LEGALE", [("Indirizzo", rec["street"]), ("CAP", rec["zip_code"]),
                         ("Comune", rec["city"]), ("Provincia", rec["province"])]),
        ("REFERENTE", [("Nome e cognome", rec["contact_name"]), ("E-mail", opt(rec["contact_email"]))]),
        ("CONDIZIONI ECONOMICHE", [
            ("Data attivazione", fmt_date(rec["start_date"], ds)),
            ("Durata contratto", f"{rec['duration_months']} mesi"),
            ("Scadenza", opt(rec["end_date"], lambda v: fmt_date(v, ds))),
            ("Canone annuale", fmt_amount(rec["annual_fee"], am) + " + IVA"),
            ("Una tantum attivazione", opt(rec["setup_fee"], lambda v: fmt_amount(v, am))),
            ("Termini di pagamento", rng.choice(PAYMENT_WORDS[rec["payment_terms_days"]])),
            ("Fatturazione", BILLING_WORDS[rec["billing_frequency"]][0].capitalize())]),
        ("SOTTOSCRIZIONE", [("Firma cliente", f"Firmato ({rec['_legal_rep']})" if rec["signed"] else "__________________"),
                            ("Data firma", opt(rec["signature_date"], lambda v: fmt_date(v, ds)))]),
    ]
    if rng.random() < 0.5:  # same form, two versions in circulation: economics before the contact
        sections[2], sections[3] = sections[3], sections[2]
    for title, rows in sections:
        d.para(title, 10, "B", 6)
        d.kv(rows)
        d.gap(3)
    d.para("I prezzi si intendono IVA esclusa. Il presente modulo costituisce parte integrante delle "
           "condizioni generali di servizio.", 7.5)
    d.output(str(path))


def render_letter(rec: dict, rng: random.Random, path: Path) -> None:
    ds, am = rng.choice(["words", "iso", "slash"]), amount_style(rng)
    d = Doc()
    d.para(OWN_COMPANY, 11, "B")
    d.para(f"{OWN_ADDRESS}  |  P.IVA {OWN_VAT}  |  contratti@esempioservizi.it", 8)
    d.gap(8)
    d.para(f"Spett.le\n{rec['company_name']}\n{rec['street']}\n{rec['zip_code']} {rec['city']} {rec['province']}", 10)
    d.gap(6)
    d.para("Oggetto: conferma d'ordine e condizioni contrattuali", 10, "B")
    d.gap(2)
    years = rec["duration_months"] // 12
    duration = (f"{years} anni" if rec["duration_months"] % 12 == 0 and years > 1 and rng.random() < 0.6
                else f"{rec['duration_months']} mesi")
    body = (f"Gentile {rec['contact_name']},\n\ncon la presente confermiamo l'attivazione dei servizi a partire "
            f"dal {fmt_date(rec['start_date'], ds)} per una durata di {duration}")
    if rec["end_date"]:
        body += f" (fino al {fmt_date(rec['end_date'], ds)})"
    body += (f". Il corrispettivo concordato è pari a {fmt_amount(rec['annual_fee'], am)} all'anno, IVA esclusa")
    if rec["setup_fee"] == 0:
        body += "; non sono previsti costi di attivazione"
    elif rec["setup_fee"]:
        body += f", a cui si aggiunge un costo di avvio di {fmt_amount(rec['setup_fee'], am)}"
    billing_word = rng.choice(BILLING_WORDS[rec["billing_frequency"]])
    if not billing_word.startswith(("ogni", "una")):
        billing_word = f"con cadenza {billing_word}"
    body += (f". Le fatture saranno emesse {billing_word} e saldate con "
             f"{rng.choice(PAYMENT_WORDS[rec['payment_terms_days']])}.")
    if rec["employees"]:
        body += f" Il servizio copre una popolazione aziendale di {rec['employees']} dipendenti."
    d.para(body, align="J")
    d.gap(3)
    billing = []
    if rec["vat_number"]:
        billing.append(f"P.IVA {fmt_vat(rec['vat_number'], 'plain')}")
    if rec["tax_code"]:
        billing.append(f"C.F. {rec['tax_code']}")
    if billing:
        d.para("Dati di fatturazione del Cliente: " + ", ".join(billing) + ".")
    if rec["contact_email"]:
        d.para(f"Indirizzo per le comunicazioni operative: {rec['contact_email']}")
    d.gap(3)
    d.para("Restiamo a disposizione per qualsiasi chiarimento. Cordiali saluti.\n\nUfficio Contratti\n" + OWN_COMPANY)
    d.gap(8)
    d.para("Per accettazione, il Cliente", 10, "B")
    if rec["signed"]:
        d.para(f"Letto, approvato e sottoscritto il {fmt_date(rec['signature_date'], ds)} da {rec['_legal_rep']} "
               f"per {rec['company_name']}  [firma]")
    else:
        d.para("______________________________")
    d.output(str(path))


def render_sheet(rec: dict, rng: random.Random, path: Path) -> None:
    ds, am = rng.choice(["dot", "words", "slash"]), amount_style(rng)
    d = Doc()
    d.para("SCHEDA ANAGRAFICA CLIENTE E CONDIZIONI", 13, "B", 7)
    d.para(f"Rif. {rec['_contract_no']}", 8)
    d.gap(4)
    ids = " / ".join(x for x in (rec["vat_number"], rec["tax_code"]) if x) or ""
    rows = [
        ("Denominazione", rec["company_name"]),
        ("P. IVA / C.F.", ids),
        ("Sede legale", f"{rec['street']} - {rec['zip_code']} {rec['city']} ({rec['province']})"),
        ("Contatto", rec["contact_name"]),
        ("Mail", opt(rec["contact_email"])),
        ("Dipendenti", opt(rec["employees"])),
        ("Decorrenza", fmt_date(rec["start_date"], ds)),
        ("Durata", f"{rec['duration_months']} mesi"),
        ("Termine", opt(rec["end_date"], lambda v: fmt_date(v, ds))),
        ("Canone/anno", fmt_amount(rec["annual_fee"], am)),
        ("Fee di attivazione", opt(rec["setup_fee"], lambda v: fmt_amount(v, am))),
        ("Pagamento", rng.choice(PAYMENT_WORDS[rec["payment_terms_days"]])),
        ("Fatturazione", BILLING_WORDS[rec["billing_frequency"]][0]),
        ("Sottoscrizione", f"Sì, in data {fmt_date(rec['signature_date'], ds)}" if rec["signed"] else "No"),
    ]
    head, tail = rows[:6], rows[6:]
    rng.shuffle(tail)  # the conditions block has no fixed order across versions of this template
    d.kv(head + tail, widths=(45, 129))
    d.gap(4)
    d.para(f"Scheda compilata dall'ufficio commerciale di {OWN_COMPANY}, importi IVA esclusa.", 7.5)
    d.output(str(path))


def render_scan(rec: dict, rng: random.Random, path: Path) -> None:
    """A photocopied, slightly rotated form typed in monospace. Text is still selectable: no OCR needed."""
    d = Doc()
    d.set_fill_color(236, 234, 228)
    d.rect(0, 0, 210, 297, style="F")
    d.set_fill_color(200, 198, 192)
    for _ in range(400):
        d.rect(rng.uniform(0, 210), rng.uniform(0, 297), 0.4, 0.4, style="F")
    v = rec["vat_number"]
    lines = [
        "PAG. 1/1                                   COPIA PER IL CLIENTE",
        "",
        "MODULO ADESIONE SERVIZI",
        "",
        f"RAG. SOCIALE ..... {rec['company_name'].upper()}",
        f"P . IVA .......... {v[:3]} {v[3:7]} {v[7:]}",
        f"SEDE ............. {rec['street'].upper().replace(' ', '  ', 1)}",
        f"                   {rec['zip_code']}  {rec['city'].upper()}  ( {rec['province']} )",
        f"REFERENTE ........ {rec['contact_name'].upper()}",
        f"MAIL ............. {rec['contact_email'] or ''}",
        "",
        f"DECORRENZA ....... {fmt_date(rec['start_date'], 'dot')}",
        f"DURATA MESI ...... {rec['duration_months']}",
        f"CANONE ANNUO ..... EUR {it_number(rec['annual_fee'])}",
        f"ATTIVAZIONE ...... {('EUR ' + it_number(rec['setup_fee'])) if rec['setup_fee'] is not None else ''}",
        f"N. DIP. .......... {rec['employees'] or ''}",
        f"PAGAM. ........... {PAYMENT_WORDS[rec['payment_terms_days']][-1].upper()}",
        f"FATTURAZ. ........ {BILLING_WORDS[rec['billing_frequency']][0].upper()}",
        "",
        f"TIMBRO E FIRMA ... (firma illeggibile)        DATA  {fmt_date(rec['signature_date'], 'slash')}",
    ]
    if rec["tax_code"]:
        lines.insert(6, f"C . F. ........... {rec['tax_code']}")
    with d.rotation(0.9, 105, 148):
        d.set_y(25)
        for line in lines:
            d.para(line, 9.5, font="Courier", h=5.5)
    d.output(str(path))


EXCEL_LABELS = [
    ("Cliente (ragione sociale)", "company_name"), ("VAT / P.IVA", "vat_number"), ("C.F.", "tax_code"),
    ("Via e civico", "street"), ("CAP", "zip_code"), ("Città", "city"), ("Prov", "province"),
    ("Referente", "contact_name"), ("Email referente", "contact_email"), ("Data inizio", "start_date"),
    ("Durata (mesi)", "duration_months"), ("Data fine", "end_date"), ("Canone annuo (EUR)", "annual_fee"),
    ("Setup (EUR)", "setup_fee"), ("Dipendenti", "employees"), ("Pagamento (gg)", "payment_terms_days"),
    ("Fatturazione", "billing_frequency"), ("Firmato", "signed"), ("Data firma", "signature_date"),
]


def render_excel(rec: dict, rng: random.Random, path: Path, horizontal: bool) -> None:
    """Activation forms that arrive as spreadsheets: one vertical label/value form, one export-style row."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Attivazione"
    ws["A1"] = "SCHEDA DI ATTIVAZIONE CLIENTE"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"Compilare e restituire al Fornitore ({OWN_COMPANY}). Importi IVA esclusa."

    def value(field):
        v = rec[field]
        if field == "signed":
            return "Sì" if v else "No"
        if field == "vat_number" and v:
            return rng.choice([v, f"IT{v}"])
        if field == "billing_frequency":
            return v.capitalize()
        return v

    fill = PatternFill("solid", fgColor="DDE6F0")
    if horizontal:
        for col, (label, field) in enumerate(EXCEL_LABELS, start=1):
            head = ws.cell(row=4, column=col, value=label)
            head.font, head.fill = Font(bold=True), fill
            cell = ws.cell(row=5, column=col, value=value(field))
            if isinstance(rec[field], date):
                cell.number_format = "DD/MM/YYYY"
            ws.column_dimensions[head.column_letter].width = max(12, len(label) + 2)
    else:
        for row, (label, field) in enumerate(EXCEL_LABELS, start=4):
            head = ws.cell(row=row, column=1, value=label)
            head.font, head.fill = Font(bold=True), fill
            cell = ws.cell(row=row, column=2, value=value(field))
            if isinstance(rec[field], date):
                cell.number_format = "DD/MM/YYYY"
            elif field in ("annual_fee", "setup_fee"):
                cell.number_format = "#,##0.00"
        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 42
    wb.save(path)


RENDERERS = {"contract": render_contract, "form": render_form, "letter": render_letter,
             "sheet": render_sheet, "scan": render_scan}


# ---------------------------------------------------------------- main

def truth_json(rec: dict) -> dict:
    fields = ContractFields(**{k: v for k, v in rec.items() if not k.startswith("_")})
    return fields.model_dump(mode="json")


def main(samples_dir: Path = ROOT / "samples", truth_dir: Path = ROOT / "ground_truth", seed: int = SEED) -> list[dict]:
    samples_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    builder = Builder(seed)
    written = []
    for i, (template, planted) in enumerate(PLAN, start=1):
        rec = builder.record(planted)
        doc_id = f"doc_{i:02d}"
        if template.startswith("excel"):
            path = samples_dir / f"{doc_id}.xlsx"
            render_excel(rec, builder.rng, path, horizontal=template == "excel_h")
        else:
            path = samples_dir / f"{doc_id}.pdf"
            RENDERERS[template](rec, builder.rng, path)
        truth = {"doc_id": doc_id, "file": path.name, "template": template,
                 "expected_issues": [planted] if planted else [], "fields": truth_json(rec)}
        (truth_dir / f"{doc_id}.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(truth)
    print(f"Wrote {len(written)} documents to {samples_dir.name}/ and their ground truth to {truth_dir.name}/")
    return written


if __name__ == "__main__":
    main()
