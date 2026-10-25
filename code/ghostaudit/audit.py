"""Per-case audit: reconstruct each linked resource's state at audit_at and emit steps."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .classifier import is_deletion_request
from .identity import contains_identifier, record_matches_identifiers, resolve_subject
from .io_utils import parse_ts
from .policy import category_for_record, required_action
from .state import (
    flow_enabled, has_direct_identifiers, identifier_exposure, lookup_state,
    observation_time, record_present, restore_allowed,
)

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# Operation order within each phase, as listed in the problem statement.
OPERATION_ORDER = {
    "FINDING": ("REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW",
                "REPORT_RESTORE_RISK"),
    "REMEDIATION": ("DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD",
                    "ANONYMIZE_RECORD"),
    "VERIFICATION": ("VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"),
}

# Records retained by policy: omission from a snapshot never declares them missing.
RETAINED_SYSTEMS = frozenset({"security_service"})

_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
_LINK_DETAILS = re.compile(
    r"linked (\S+) \(.*?\) to (\S+) and (user_\w+)|confirmed (\S+) \(.*?\) belongs only to (\S+) and (user_\w+)"
)


@dataclass
class AuditContext:
    """Dataset-wide inputs shared by every case."""
    identities: list
    audit_log: list
    identity_index: dict
    rules: dict
    evidence: list
    system_activity: list
    coverage: list = field(init=False)

    def __post_init__(self):
        self.coverage = [r for r in self.evidence if r.get("kind") == "coverage"]


# --------------------------------------------------------------------------- evidence selection

def linked_resources(identifiers, audit_at, system_activity):
    """Resource ids that a data-catalog LINK_RESOURCE_IDENTITY event tied to the subject by audit_at."""
    linked = set()
    for row in system_activity:
        if row.get("event_type") != "LINK_RESOURCE_IDENTITY":
            continue
        event_time = parse_ts(row.get("event_time", ""))
        if event_time is not None and audit_at is not None and event_time > audit_at:
            continue
        m = _LINK_DETAILS.search(row.get("details", ""))
        if not m:
            continue
        resource_id, email, user_id = [g for g in m.groups() if g]
        if any(contains_identifier(email, i) or contains_identifier(user_id, i) for i in identifiers):
            linked.add(resource_id)
    return linked


def _admissible(record, audit_at):
    """Captured (and, if stated, observed) at or before audit_at."""
    if audit_at is None:
        return True
    for moment in (record["captured_at"], observation_time(record)):
        if moment is not None and moment > audit_at:
            return False
    return True


def find_relevant_records(identifiers, audit_at, evidence, system_activity):
    """Admissible evidence records that carry the subject's identifiers or a catalog-linked resource."""
    linked = linked_resources(identifiers, audit_at, system_activity)
    return [
        rec for rec in evidence
        if rec.get("kind") != "coverage" and _admissible(rec, audit_at)
        and (rec["resource_id"] in linked or record_matches_identifiers(rec, identifiers))
    ]


def _captured(record):
    return record["captured_at"] or _EPOCH


def latest_observations(relevant, coverage, audit_at):
    """The observation that decides each (system, resource_id) at audit_at.

    Each store (primary, or a named replica) is judged by its latest admissible
    observation; a later covering snapshot that no longer lists the resource counts as
    an absent observation. The resource remains if any store's latest observation still
    holds it, so a primary readback cannot hide a replica copy."""
    by_resource = {}
    for rec in relevant:
        key = (rec["system"], rec["resource_id"])
        by_resource.setdefault(key, {}).setdefault(rec.get("store", "primary"), []).append(rec)

    chosen = {}
    for (system, resource_id), stores in by_resource.items():
        per_store = []
        for store, recs in stores.items():
            latest = max(_captured(r) for r in recs)
            per_store.append(
                _snapshot_absence(system, store, resource_id, latest, coverage, audit_at)
                or _prefer_exposed([r for r in recs if _captured(r) == latest])
            )
        per_store.sort(key=_captured, reverse=True)
        chosen[(system, resource_id)] = _prefer_exposed(per_store)
    return chosen


def _prefer_exposed(records):
    """Among simultaneous observations prefer one showing exposure, then presence."""
    present = [r for r in records if record_present(r)]
    exposed = [r for r in present if has_direct_identifiers(r)]
    return (exposed or present or records)[0]


def _snapshot_absence(system, store, resource_id, latest, coverage, audit_at):
    """A synthetic NOT_FOUND observation from the latest covering snapshot that is newer
    than `latest`, admissible, and no longer lists the resource; None otherwise."""
    if system in RETAINED_SYSTEMS:
        return None
    later = [
        snap for snap in coverage
        if snap["captured_at"] and snap["captured_at"] > latest
        and (audit_at is None or snap["captured_at"] <= audit_at)
        and (system, store) in snap["stores"] and resource_id not in snap["resource_ids"]
    ]
    if not later:
        return None
    snap = max(later, key=lambda s: s["captured_at"])
    return {"file_id": snap["file_id"], "captured_at": snap["captured_at"], "system": system,
            "store": store, "resource_id": resource_id, "record": {"observed_state": "NOT_FOUND"},
            "text": ""}


# --------------------------------------------------------------------------- steps

class StepPlan:
    """Collects findings, remediation and verification for one case."""

    def __init__(self):
        self.steps = {phase: [] for phase in PHASES}

    def _add(self, phase, operation, record, state, evidence_id):
        self.steps[phase].append({
            "phase": phase, "system_id": record["system"], "resource_id": record["resource_id"],
            "operation": operation, "state": state, "evidence_id": evidence_id,
        })

    def violation(self, record, finding, fix, check):
        """A finding citing evidence, its remediation, and the check that must pass after it.
        Each argument is an (operation, state) pair."""
        self._add("FINDING", finding[0], record, finding[1], record["file_id"])
        self._add("REMEDIATION", fix[0], record, fix[1], "")
        self._add("VERIFICATION", check[0], record, check[1], "")

    def compliant(self, record, operation, state):
        """A verification proven by the cited evidence."""
        self._add("VERIFICATION", operation, record, state, record["file_id"])

    @property
    def has_findings(self):
        return bool(self.steps["FINDING"])

    @property
    def has_verified_state(self):
        return bool(self.steps["VERIFICATION"])

    def ordered(self, future_verifications):
        """Steps ordered by phase, operation, system_id and resource_id, numbered from 1.
        For INCOMPLETE cases verification steps describe future checks (no evidence)."""
        if future_verifications:
            for step in self.steps["VERIFICATION"]:
                step["evidence_id"] = ""
        ordered = []
        for phase in PHASES:
            order = OPERATION_ORDER[phase]
            for step in sorted(self.steps[phase],
                               key=lambda s: (order.index(s["operation"]), s["system_id"], s["resource_id"])):
                ordered.append({"step_no": len(ordered) + 1, **step})
        return ordered


def build_steps(findings, remediations, verifications):
    """Order and number pre-built steps (helper kept for tests and tooling)."""
    plan = StepPlan()
    for phase, steps in zip(PHASES, (findings, remediations, verifications)):
        plan.steps[phase].extend(steps)
    return plan.ordered(future_verifications=False)


def plan_resource(plan, record, rules):
    """Apply deletion_rules.csv to one resource's deciding observation."""
    category = category_for_record(record)
    present = record_present(record)

    if category == "DATA_FLOW":
        enabled = flow_enabled(record)
        if enabled if enabled is not None else present:
            plan.violation(record, ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"), ("DISABLE_DATA_FLOW", "DISABLED"),
                           ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"))
        else:
            plan.compliant(record, "VERIFY_DATA_FLOW_DISABLED", "DISABLED")
    elif category == "BACKUP_ARCHIVE":
        allowed = restore_allowed(record)
        if allowed if allowed is not None else present:
            plan.violation(record, ("REPORT_RESTORE_RISK", "ACTIVE"), ("BLOCK_RESTORE", "BLOCKED"),
                           ("VERIFY_RESTORE_BLOCKED", "BLOCKED"))
        else:
            plan.compliant(record, "VERIFY_RESTORE_BLOCKED", "BLOCKED")
    elif category in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        # Retained by policy; only an observed copy proves it is still available
        if present:
            plan.compliant(record, "VERIFY_RECORD_PRESENT", "PRESENT")
    elif category == "ACCOUNT_TOMBSTONE" and lookup_state(record) != "NOT_FOUND":
        # A tombstone is DELETED by design and may keep only the stable user reference
        if has_direct_identifiers(record):
            plan.violation(record, ("REPORT_EXPOSED_PII", "PRESENT"), ("REDACT_RECORD", "REDACTED"),
                           ("VERIFY_RECORD_REDACTED", "REDACTED"))
        else:
            plan.compliant(record, "VERIFY_RECORD_REDACTED", "REDACTED")
    elif not present:
        plan.compliant(record, "VERIFY_RECORD_ABSENT", "ABSENT")
    else:
        _plan_remaining_record(plan, record, required_action(category, record, rules))


def _plan_remaining_record(plan, record, action):
    exposure = identifier_exposure(record)
    remaining = (("REPORT_REMAINING_RECORD", "PRESENT"), ("DELETE_RECORD", "ABSENT"),
                 ("VERIFY_RECORD_ABSENT", "ABSENT"))
    if action in ("REDACT", "ANONYMIZE") and exposure is None:
        # The copy remains but its fields are not visible, so treatment cannot be verified
        plan.violation(record, *remaining)
    elif action == "REDACT":
        if exposure:
            plan.violation(record, ("REPORT_EXPOSED_PII", "PRESENT"), ("REDACT_RECORD", "REDACTED"),
                           ("VERIFY_RECORD_REDACTED", "REDACTED"))
        else:
            plan.compliant(record, "VERIFY_RECORD_REDACTED", "REDACTED")
    elif action == "ANONYMIZE":
        if exposure:
            plan.violation(record, ("REPORT_EXPOSED_PII", "PRESENT"), ("ANONYMIZE_RECORD", "ANONYMIZED"),
                           ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"))
        else:
            plan.compliant(record, "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED")
    elif action == "RETAIN":
        plan.compliant(record, "VERIFY_RECORD_PRESENT", "PRESENT")
    else:
        plan.violation(record, *remaining)


# --------------------------------------------------------------------------- case

def audit_case(case, ctx):
    """Return (case_id, deletion_status, steps) for one deletion request."""
    case_id = case["case_id"].strip()
    if not is_deletion_request(case.get("request_text", "")):
        return case_id, "NOT_APPLICABLE", []

    audit_at = parse_ts(case.get("audit_at", ""))
    _, identifiers = resolve_subject(case.get("email", ""), ctx.identity_index, ctx.identities, ctx.audit_log)
    relevant = find_relevant_records(identifiers, audit_at, ctx.evidence, ctx.system_activity)

    plan = StepPlan()
    for (_, resource_id), record in sorted(latest_observations(relevant, ctx.coverage, audit_at).items()):
        if resource_id.strip():
            plan_resource(plan, record, ctx.rules)

    # COMPLETE when admissible evidence shows the subject's records in their required state
    # and no copy, exposure, data flow or restore path remains. This includes a case whose
    # only linked record is a retained audit record that is still present (the public
    # sample answers treat such cases as COMPLETE).
    if not plan.has_findings and plan.has_verified_state:
        return case_id, "COMPLETE", plan.ordered(future_verifications=False)
    # A violation was found, or no admissible evidence links any record to the subject:
    # INCOMPLETE, with remediation and checks as future work. No finding is invented.
    return case_id, "INCOMPLETE", plan.ordered(future_verifications=True)
