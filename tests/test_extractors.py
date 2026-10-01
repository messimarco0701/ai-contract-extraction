"""
Extractor tests. None of them touches the network: the Claude extractor gets a
fake client that records the request and returns a canned response.
"""

import json
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2 as httpx  # the HTTP library the anthropic 1.x SDK is built on
import pytest

from contract_extraction import generate
from contract_extraction.evaluate import load_truth
from contract_extraction.extractors import ClaudeExtractor, RuleBasedExtractor, get_extractor
from contract_extraction.pipeline import ROOT, load_config, run, write_outputs
from contract_extraction.schema import ContractFields
from contract_extraction.text import read_text
from contract_extraction.validation import normalize

CONFIG = load_config()
TRUTH = load_truth()


# ---------------------------------------------------------------- baseline

@pytest.mark.parametrize("doc_id", ["doc_02", "doc_05", "doc_04"])  # activation form, Excel form, client sheet
def test_baseline_on_structured_documents(doc_id):
    doc = TRUTH[doc_id]
    result = RuleBasedExtractor(CONFIG).extract(read_text(ROOT / "samples" / doc["file"]))
    got, expected = normalize(result.fields), doc["record"]
    for field in ("company_name", "vat_number", "zip_code", "province", "start_date",
                  "duration_months", "annual_fee", "contact_email", "signed"):
        assert getattr(got, field) == getattr(expected, field), field


def test_baseline_skips_the_supplier():
    # Contracts list the supplier first: its VAT must never end up as the customer's
    doc = TRUTH["doc_01"]
    result = RuleBasedExtractor(CONFIG).extract(read_text(ROOT / "samples" / doc["file"]))
    assert result.fields.vat_number == doc["record"].vat_number
    assert result.fields.vat_number != CONFIG["own_company"]["vat_number"]


# ---------------------------------------------------------------- Claude, mocked

class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(**kwargs):
    return SimpleNamespace(messages=FakeMessages(**kwargs))


def test_claude_request_and_response():
    fields = ContractFields(company_name="Rossi Logistica S.r.l.", vat_number="01234567897",
                            start_date=date(2026, 3, 1), annual_fee=12500.0)
    client = fake_client(response=SimpleNamespace(stop_reason="end_turn", parsed_output=fields))
    result = ClaudeExtractor(CONFIG, client=client).extract("Spett.le Rossi Logistica S.r.l. ...")

    assert result.error is None
    assert result.fields == fields
    call = client.messages.calls[0]
    assert call["model"] == CONFIG["extraction"]["model"]
    assert call["output_format"] is ContractFields
    assert "<document>\nSpett.le Rossi Logistica" in call["messages"][0]["content"]
    assert "return null" in call["system"]
    assert CONFIG["own_company"]["name"] in call["system"]


def test_claude_refusal_goes_to_review():
    client = fake_client(response=SimpleNamespace(stop_reason="refusal", parsed_output=None))
    result = ClaudeExtractor(CONFIG, client=client).extract("...")
    assert result.fields == ContractFields()
    assert result.error.startswith("extraction_failed")


def test_claude_api_error_goes_to_review():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=request), body=None)
    result = ClaudeExtractor(CONFIG, client=fake_client(error=error)).extract("...")
    assert result.error == "extraction_failed: API error 529"


def test_pipeline_with_mocked_claude_marks_failures_for_review(tmp_path):
    client = fake_client(response=SimpleNamespace(stop_reason="refusal", parsed_output=None))
    df = run(ROOT / "samples", ClaudeExtractor(CONFIG, client=client), CONFIG)
    assert (df.status == "needs_review").all()
    assert len(client.messages.calls) == len(TRUTH)


# ---------------------------------------------------------------- choice and outputs

def test_auto_falls_back_to_rules_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    extractor, note = get_extractor("auto", CONFIG)
    assert isinstance(extractor, RuleBasedExtractor)
    assert "ANTHROPIC_API_KEY" in note


def test_claude_without_key_stops(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        get_extractor("claude", CONFIG)


def test_outputs_written(tmp_path):
    df = run(ROOT / "samples", RuleBasedExtractor(CONFIG), CONFIG)
    path = write_outputs(df, tmp_path)
    sheets = __import__("openpyxl").load_workbook(path).sheetnames
    assert sheets == ["Extracted", "Review queue"]
    assert (tmp_path / "extracted.csv").exists()


def test_generator_is_deterministic(tmp_path):
    generate.main(tmp_path / "samples", tmp_path / "truth")
    for path in (tmp_path / "truth").glob("*.json"):
        committed = json.loads((ROOT / "ground_truth" / path.name).read_text(encoding="utf-8"))
        assert json.loads(path.read_text(encoding="utf-8")) == committed
