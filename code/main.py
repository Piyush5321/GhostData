#!/usr/bin/env python3
"""Ghost Data Still Exists: audit every deletion request and write output.csv.

Usage (from the repository root):
    python code/main.py [--dataset DATASET_DIR] [--output OUTPUT_CSV]
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghostaudit.audit import AuditContext, audit_case  # noqa: E402
from ghostaudit.evidence import load_all_evidence  # noqa: E402
from ghostaudit.identity import build_identity_index  # noqa: E402
from ghostaudit.io_utils import DATASET_DIR, OUTPUT_PATH, load_csv  # noqa: E402
from ghostaudit.linkage import activity_links  # noqa: E402
from ghostaudit.policy import load_rules  # noqa: E402

OUTPUT_COLUMNS = ("case_id", "deletion_status", "steps_json")


REQUEST_FILES = ("deletion_requests.csv", "sample_deletion_requests.csv")


def build_context(dataset_dir):
    """Load every participant-facing input once and link workflow activity to requests.

    Activity is linked over all request files together: the more requests compete for the
    same events, the more of them are resolved by the uniqueness constraint."""
    identities = load_csv("user_identities.csv", dataset_dir)
    audit_log = load_csv("user_audit_log.csv", dataset_dir)
    system_activity = load_csv("system_activity.csv", dataset_dir)
    evidence = load_all_evidence(load_csv("evidence_files.csv", dataset_dir), system_activity, dataset_dir)
    ctx = AuditContext(
        identities=identities,
        audit_log=audit_log,
        identity_index=build_identity_index(identities, audit_log),
        rules=load_rules(load_csv("deletion_rules.csv", dataset_dir)),
        evidence=evidence,
        system_activity=system_activity,
    )
    requests = [case for name in REQUEST_FILES if (Path(dataset_dir) / name).exists()
                for case in load_csv(name, dataset_dir)]
    ctx.activity_links = activity_links(requests, ctx)
    return ctx


def run(dataset_dir=DATASET_DIR, output_path=OUTPUT_PATH, requests_file="deletion_requests.csv"):
    """Audit all requests and write the sorted output CSV. Returns the number of rows."""
    ctx = build_context(dataset_dir)
    rows = []
    for case in load_csv(requests_file, dataset_dir):
        case_id, status, steps = audit_case(case, ctx)
        rows.append({
            "case_id": case_id,
            "deletion_status": status,
            "steps_json": json.dumps(steps, ensure_ascii=False, separators=(",", ":")),
        })
    rows.sort(key=lambda r: r["case_id"])
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR, help="dataset directory")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="output CSV path")
    args = parser.parse_args(argv)
    count = run(args.dataset, args.output)
    print(f"Wrote {count} rows to {args.output}")


if __name__ == "__main__":
    main()
