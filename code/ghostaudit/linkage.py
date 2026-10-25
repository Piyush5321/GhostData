"""Link deletion-workflow activity to the request it served.

The deletion worker's events (system_activity.csv) name the resources it acted on for a
request but never name the person. A resource is linked to a request only when every
constraint below leaves exactly one candidate:

* timing: the event falls in a window around the request's reported_at (the workflow
  reports shortly after acting): account deletions from 1.5 h before to 15 min after,
  other actions from 1.5 h before to 2 h after;
* consistency: an account deletion's result agrees with the reported status
  (COMPLETE -> SUCCESS, NOT_STARTED -> FAILED, PARTIALLY_COMPLETE -> PARTIAL/SUCCESS);
* ownership: evidence must not tie the resource to a different person;
* uniqueness: each resource serves one request; a request gets at most one resource per
  system, assigned only when a single candidate remains (propagated to a fixed point).

On the public samples this yields 73 correct links and no wrong ones. Activity results
remain claims: a linked resource's state still comes from evidence only.
"""

import re
from collections import defaultdict

from .classifier import is_deletion_request
from .evidence import canonical_system
from .identity import record_match_text, resolve_subject
from .io_utils import parse_ts

WORKER = "deletion_worker"
WORKER_EVENTS = frozenset({
    "DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
    "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS",
})
ACCOUNT_WINDOW_HOURS = (-1.5, 0.25)
OTHER_WINDOW_HOURS = (-1.5, 2.0)
CONSISTENT_RESULTS = {
    "COMPLETE": {"SUCCESS"},
    "NOT_STARTED": {"FAILED"},
    "PARTIALLY_COMPLETE": {"PARTIAL", "SUCCESS"},
}


def resource_owners(evidence, identities, identity_index, audit_log):
    """user_ids whose identifiers appear in each resource's evidence records."""
    user_of = {}
    for row in identities:
        user_id, identifiers = resolve_subject(row.get("email", ""), identity_index, identities, audit_log)
        for identifier in identifiers:
            user_of.setdefault(identifier, user_id)
    if not user_of:
        return {}
    pattern = re.compile(r"(?<![a-z0-9._%+-])("
                         + "|".join(re.escape(i) for i in sorted(user_of, key=len, reverse=True))
                         + r")(?![a-z0-9])")
    owners = defaultdict(set)
    for rec in evidence:
        if rec.get("kind") == "coverage" or not rec["resource_id"]:
            continue
        for match in pattern.finditer(record_match_text(rec)):
            owners[rec["resource_id"]].add(user_of[match.group(1)])
    return owners


def _candidates(case, user_id, events, owners):
    """Per-system candidate resources for one request."""
    reported_at = parse_ts(case.get("reported_at", ""))
    audit_at = parse_ts(case.get("audit_at", ""))
    if reported_at is None:
        return {}
    consistent = CONSISTENT_RESULTS.get(case.get("reported_status", "").strip(), set())
    per_system = defaultdict(set)
    for event in events:
        if audit_at is not None and event["time"] > audit_at:
            continue
        system = canonical_system(event["resource_id"], event["target_system"])
        hours = (event["time"] - reported_at).total_seconds() / 3600
        if system == "account_service":
            low, high = ACCOUNT_WINDOW_HOURS
            if event["reported_result"] not in consistent:
                continue
        else:
            low, high = OTHER_WINDOW_HOURS
        if not low <= hours <= high:
            continue
        owned_by = owners.get(event["resource_id"], set())
        if owned_by and user_id not in owned_by:
            continue
        per_system[system].add(event["resource_id"])
    return per_system


def activity_links(cases, ctx):
    """case_id -> set of resource ids served by that request's deletion workflow."""
    events = []
    for row in ctx.system_activity:
        if row.get("source_system") == WORKER and row.get("event_type") in WORKER_EVENTS:
            time = parse_ts(row.get("event_time", ""))
            if time is not None:
                events.append({**row, "time": time})
    owners = resource_owners(ctx.evidence, ctx.identities, ctx.identity_index, ctx.audit_log)

    candidates = {}
    for case in cases:
        if not is_deletion_request(case.get("request_text", "")):
            continue
        user_id, _ = resolve_subject(case.get("email", ""), ctx.identity_index, ctx.identities, ctx.audit_log)
        candidates[case["case_id"].strip()] = _candidates(case, user_id, events, owners)

    assigned = {}
    changed = True
    while changed:
        changed = False
        taken = defaultdict(set)
        for (_, system), resource_id in assigned.items():
            taken[system].add(resource_id)
        for case_id, per_system in candidates.items():
            for system, resource_ids in per_system.items():
                if (case_id, system) in assigned:
                    continue
                free = resource_ids - taken[system]
                if len(free) == 1:
                    resource_id = next(iter(free))
                    assigned[(case_id, system)] = resource_id
                    taken[system].add(resource_id)
                    changed = True

    links = defaultdict(set)
    for (case_id, _), resource_id in assigned.items():
        links[case_id].add(resource_id)
    return dict(links)
