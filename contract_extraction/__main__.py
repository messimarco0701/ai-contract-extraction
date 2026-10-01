"""
Command line entry point.

  python -m contract_extraction generate
  python -m contract_extraction run --input samples/ --extractor auto|claude|rules
  python -m contract_extraction evaluate --extractor auto|claude|rules
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import evaluate, generate
from .extractors import get_extractor
from .pipeline import ROOT, load_config, run, write_outputs


def main(argv: list[str] | None = None) -> None:
    # Windows consoles default to cp1252 and choke on characters like the euro sign
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="python -m contract_extraction")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate", help="create the synthetic documents and ground truth")
    run_p = sub.add_parser("run", help="extract, validate and write output/extracted.xlsx")
    run_p.add_argument("--input", type=Path, default=ROOT / "samples")
    run_p.add_argument("--output", type=Path, default=ROOT / "output")
    eval_p = sub.add_parser("evaluate", help="compare an extractor with the ground truth")
    for p in (run_p, eval_p):
        p.add_argument("--extractor", choices=["auto", "claude", "rules"], default="auto")
    args = parser.parse_args(argv)

    if args.command == "generate":
        generate.main()
        return

    config = load_config()
    extractor, note = get_extractor(args.extractor, config)
    if note:
        print(note)
    if args.extractor != "rules" and extractor.name == "claude":
        print(f"Using Claude ({config['extraction']['model']}).")

    if args.command == "evaluate":
        print(evaluate.main(extractor, config))
        print(f"Saved to reports/evaluation_{extractor.name}.csv")
        return

    df = run(args.input, extractor, config)
    path = write_outputs(df, args.output)
    print(f"Documents: {len(df)}   ok: {(df.status == 'ok').sum()}   "
          f"needs_review: {(df.status == 'needs_review').sum()}\n")
    review = df[df.status == "needs_review"][["doc_id", "issues"]]
    if not review.empty:
        print("Review queue:")
        for doc_id, issues in review.itertuples(index=False):
            print(f"  {doc_id}  {issues}")
    print(f"\nWritten {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path} "
          f"and extracted.csv")


if __name__ == "__main__":
    main()
