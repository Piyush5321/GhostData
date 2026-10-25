"""Output contract validation and scoring against the public sample answers."""

import json
from collections import Counter

from .audit import OPERATION_ORDER, PHASES
from .io_utils import parse_ts

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
OPERATION_STATES = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}


def validate_output(rows, requests, evidence_manifest):
    """Check output rows against the submission contract.

    Returns a list of (case_id, problem) tuples; an empty list means the output is valid."""
    problems = []
    expected_ids = sorted(r["case_id"] for r in requests)
    ids = [r["case_id"] for r in rows]
    if ids != expected_ids:
        problems.append(("*", "case ids must match deletion_requests.csv exactly and be sorted"))
    audit_at = {r["case_id"]: parse_ts(r["audit_at"]) for r in requests}
    captured = {r["file_id"]: parse_ts(r["captured_at"]) for r in evidence_manifest}

    for row in rows:
        case_id, status = row["case_id"], row["deletion_status"]
        if status not in STATUSES:
            problems.append((case_id, f"invalid status {status!r}"))
            continue
        try:
            steps = json.loads(row["steps_json"])
        except ValueError:
            problems.append((case_id, "steps_json is not valid JSON"))
            continue
        problems.extend((case_id, p) for p in _step_problems(status, steps, audit_at.get(case_id), captured))
    return problems


def _step_problems(status, steps, audit_at, captured):
    if not isinstance(steps, list):
        return ["steps_json must be a JSON array"]
    if status == "NOT_APPLICABLE":
        return [] if steps == [] else ["NOT_APPLICABLE must have []"]
    problems = []
    if [s.get("step_no") for s in steps] != list(range(1, len(steps) + 1)):
        problems.append("step_no must start at 1 and increase consecutively")
    expected = sorted(steps, key=lambda s: _order_key(s))
    if [id(s) for s in expected] != [id(s) for s in steps]:
        problems.append("steps are not in phase / operation / system_id / resource_id order")
    for step in steps:
        if tuple(sorted(step)) != tuple(sorted(STEP_FIELDS)):
            problems.append(f"step {step.get('step_no')} must have exactly the 7 required fields")
            continue
        if OPERATION_STATES.get(step["operation"]) != step["state"] or \
                step["operation"] not in OPERATION_ORDER.get(step["phase"], ()):
            problems.append(f"step {step['step_no']} has an invalid phase/operation/state")
        evidence_id = step["evidence_id"]
        needs_evidence = step["phase"] == "FINDING" or status == "COMPLETE"
        if needs_evidence:
            if evidence_id not in captured:
                problems.append(f"step {step['step_no']} must cite a manifest evidence file")
            elif audit_at and captured[evidence_id] and captured[evidence_id] > audit_at:
                problems.append(f"step {step['step_no']} cites evidence captured after audit_at")
        elif evidence_id:
            problems.append(f"step {step['step_no']} describes future work and must not cite evidence")
    if status == "COMPLETE" and any(s.get("phase") != "VERIFICATION" for s in steps):
        problems.append("COMPLETE may contain verification steps only")
    return problems


def _order_key(step):
    phase = step.get("phase")
    phase_index = PHASES.index(phase) if phase in PHASES else len(PHASES)
    operations = OPERATION_ORDER.get(phase, ())
    op_index = operations.index(step.get("operation")) if step.get("operation") in operations else len(operations)
    return phase_index, op_index, str(step.get("system_id")), str(step.get("resource_id"))


def _step_key(step):
    return tuple(str(step.get(f)) for f in STEP_FIELDS[1:])


def score_against_gold(predicted, gold):
    """Sample metrics. Both arguments map case_id -> (status, steps).

    status:   share of cases with the gold deletion_status
    sequence: share of cases whose ordered step list matches exactly
    steps:    precision / recall of steps (all fields except step_no), overall and per phase
    fields:   position-aligned agreement over the 7 step fields
    """
    status_hits = sequence_hits = field_hits = field_total = 0
    confusion = Counter()
    phase_counts = {phase: Counter() for phase in PHASES}
    for case_id, (gold_status, gold_steps) in gold.items():
        status, steps = predicted[case_id]
        confusion[(gold_status, status)] += 1
        status_hits += status == gold_status
        sequence_hits += [_step_key(s) for s in steps] == [_step_key(s) for s in gold_steps]
        for phase in PHASES:
            g = {_step_key(s) for s in gold_steps if s["phase"] == phase}
            p = {_step_key(s) for s in steps if s["phase"] == phase}
            phase_counts[phase].update(tp=len(g & p), fp=len(p - g), fn=len(g - p))
        for i in range(max(len(steps), len(gold_steps))):
            for f in STEP_FIELDS:
                field_total += 1
                field_hits += (i < len(steps) and i < len(gold_steps)
                               and str(steps[i][f]) == str(gold_steps[i][f]))
    total = Counter()
    for counts in phase_counts.values():
        total.update(counts)
    n = len(gold)
    return {
        "cases": n,
        "status_accuracy": status_hits / n,
        "sequence_accuracy": sequence_hits / n,
        "step_precision": _ratio(total["tp"], total["tp"] + total["fp"]),
        "step_recall": _ratio(total["tp"], total["tp"] + total["fn"]),
        "field_accuracy": _ratio(field_hits, field_total),
        "per_phase": {
            phase: {"precision": _ratio(c["tp"], c["tp"] + c["fp"]),
                    "recall": _ratio(c["tp"], c["tp"] + c["fn"]), **c}
            for phase, c in phase_counts.items()
        },
        "status_confusion": {f"{g}->{p}": v for (g, p), v in sorted(confusion.items())},
        "status_mismatches": sorted(c for c, (g, _) in gold.items() if predicted[c][0] != g),
    }


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else 1.0
