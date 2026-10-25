#!/usr/bin/env python3
"""Score the agent on the public samples and validate output.csv against the contract.

Usage (from the repository root):
    python code/evaluate.py [--dataset DATASET_DIR] [--output OUTPUT_CSV]

Exit status is 1 if output.csv violates the submission contract.
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghostaudit.audit import audit_case  # noqa: E402
from ghostaudit.io_utils import DATASET_DIR, OUTPUT_PATH, load_csv  # noqa: E402
from ghostaudit.validation import score_against_gold, validate_output  # noqa: E402
from main import build_context  # noqa: E402


def sample_metrics(dataset_dir):
    ctx = build_context(dataset_dir)
    gold = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
            for r in load_csv("sample_output.csv", dataset_dir)}
    predicted = {}
    for case in load_csv("sample_deletion_requests.csv", dataset_dir):
        case_id, status, steps = audit_case(case, ctx)
        predicted[case_id] = (status, steps)
    return score_against_gold(predicted, gold)


def output_summary(output_path, dataset_dir):
    with open(output_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    problems = validate_output(rows, load_csv("deletion_requests.csv", dataset_dir),
                               load_csv("evidence_files.csv", dataset_dir))
    statuses = Counter(r["deletion_status"] for r in rows)
    incomplete = [json.loads(r["steps_json"]) for r in rows if r["deletion_status"] == "INCOMPLETE"]
    shapes = Counter(
        "empty" if not steps else
        "with findings" if any(s["phase"] == "FINDING" for s in steps) else "verification only"
        for steps in incomplete
    )
    return len(rows), statuses, shapes, problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the deletion-audit agent.")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)

    m = sample_metrics(args.dataset)
    print(f"Public samples ({m['cases']} cases)")
    print(f"  status accuracy    {m['status_accuracy']:.3f}")
    print(f"  sequence accuracy  {m['sequence_accuracy']:.3f}")
    print(f"  step precision     {m['step_precision']:.3f}")
    print(f"  step recall        {m['step_recall']:.3f}")
    print(f"  field accuracy     {m['field_accuracy']:.3f}")
    for phase, c in m["per_phase"].items():
        print(f"  {phase:<12} P={c['precision']:.3f} R={c['recall']:.3f} "
              f"(tp {c['tp']}, fp {c['fp']}, fn {c['fn']})")
    print(f"  status confusion   {m['status_confusion']}")
    print(f"  status mismatches  {', '.join(m['status_mismatches']) or 'none'}")

    count, statuses, shapes, problems = output_summary(args.output, args.dataset)
    print(f"\n{args.output.name}: {count} rows; " + ", ".join(f"{s} {statuses[s]}" for s in sorted(statuses)))
    print("  INCOMPLETE: " + ", ".join(f"{k} {v}" for k, v in sorted(shapes.items())))
    print(f"  contract violations: {len(problems)}")
    for case_id, problem in problems[:20]:
        print(f"    {case_id}: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
