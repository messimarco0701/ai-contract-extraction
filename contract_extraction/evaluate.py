"""
Measures an extractor against the ground truth, field by field.

For every field of every document the outcome is one of:
  correct       both have a value and it matches
  wrong         both have a value and it doesn't match
  missed        the document has a value, the extractor returned nothing
  invented      the document has no value, the extractor returned one
  correct_null  neither has a value

Precision answers "when it gives me a value, how often is it right" and recall
"of the values in the documents, how many did it find". For loading data into
a CRM, precision matters more: a missed field is visible, a wrong one is not.

The last block is the number I care about most: how many records the
validation rules let through as "ok" while one of their fields was wrong.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pandas as pd

from .extractors import Extractor
from .pipeline import ROOT, run
from .schema import FIELDS, ContractFields
from .validation import normalize


def load_truth(truth_dir: Path = ROOT / "ground_truth") -> dict[str, dict]:
    docs = {}
    for path in sorted(truth_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["record"] = normalize(ContractFields(**doc["fields"]))
        docs[doc["doc_id"]] = doc
    return docs


def _canon(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (bool, date)):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    return re.sub(r"[^\w@]+", " ", str(value).casefold()).strip()


def outcome(predicted, expected) -> str:
    p, e = _canon(predicted), _canon(expected)
    if e is None:
        return "correct_null" if p is None else "invented"
    if p is None:
        return "missed"
    return "correct" if p == e else "wrong"


def evaluate(extracted: pd.DataFrame, truth: dict[str, dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    for rec in extracted.to_dict("records"):
        expected = truth[rec["doc_id"]]["record"]
        for field in FIELDS:
            rows.append({"doc_id": rec["doc_id"], "field": field,
                         "outcome": outcome(rec[field], getattr(expected, field))})
    detail = pd.DataFrame(rows)
    counts = detail.pivot_table(index="field", columns="outcome", aggfunc="size", fill_value=0)
    for col in ("correct", "wrong", "missed", "invented", "correct_null"):
        if col not in counts:
            counts[col] = 0
    counts = counts.reindex(FIELDS)[["correct", "wrong", "missed", "invented", "correct_null"]]
    counts.loc["ALL FIELDS"] = counts.sum()
    c = counts
    counts["accuracy"] = (c.correct + c.correct_null) / c[["correct", "wrong", "missed", "invented", "correct_null"]].sum(axis=1)
    counts["precision"] = c.correct / (c.correct + c.wrong + c.invented)
    counts["recall"] = c.correct / (c.correct + c.wrong + c.missed)
    return counts.reset_index(), detail


def review_summary(extracted: pd.DataFrame, detail: pd.DataFrame, truth: dict[str, dict]) -> dict:
    errors = detail[detail.outcome.isin(["wrong", "invented"])].groupby("doc_id").size()
    ok = extracted[extracted.status == "ok"]
    planted = {d: t["expected_issues"] for d, t in truth.items() if t["expected_issues"]}
    caught = sum(1 for d, issues in planted.items()
                 if set(issues) <= set(extracted.set_index("doc_id").loc[d, "issues"].split("; ")))
    return {
        "documents": len(extracted),
        "ok": len(ok),
        "needs_review": int((extracted.status == "needs_review").sum()),
        "ok_but_wrong": int(ok.doc_id.isin(errors.index).sum()),
        "planted_problems": len(planted),
        "planted_caught": caught,
    }


def format_report(extractor_name: str, metrics: pd.DataFrame, summary: dict) -> str:
    table = metrics.to_string(index=False, formatters={
        "accuracy": "{:.0%}".format, "precision": "{:.0%}".format, "recall": "{:.0%}".format})
    return (
        f"Extractor: {extractor_name}   Documents: {summary['documents']}\n\n"
        f"{table}\n\n"
        f"Status after validation:  ok {summary['ok']}   needs_review {summary['needs_review']}\n"
        f"Records marked ok that still contain a wrong or invented value: {summary['ok_but_wrong']}\n"
        f"Planted problems flagged with the expected reason: "
        f"{summary['planted_caught']} of {summary['planted_problems']}\n"
    )


def main(extractor: Extractor, config: dict, input_dir: Path = ROOT / "samples",
         reports_dir: Path = ROOT / "reports") -> str:
    truth = load_truth()
    extracted = run(input_dir, extractor, config)
    metrics, detail = evaluate(extracted, truth)
    summary = review_summary(extracted, detail, truth)
    report = format_report(extractor.name, metrics, summary)
    reports_dir.mkdir(exist_ok=True)
    metrics.to_csv(reports_dir / f"evaluation_{extractor.name}.csv", index=False, float_format="%.3f")
    detail.to_csv(reports_dir / f"evaluation_{extractor.name}_detail.csv", index=False)
    (reports_dir / f"evaluation_{extractor.name}.txt").write_text(report, encoding="utf-8")
    return report
