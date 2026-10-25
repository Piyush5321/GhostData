import unittest

from tests.helpers import record, ts
from ghostaudit.audit import AuditContext, audit_case, build_steps, find_relevant_records, latest_observations
from ghostaudit.evidence import coverage_record
from ghostaudit.identity import build_identity_index
from ghostaudit.state import record_present

RULES = {
    "USER_PROFILE": {"data_category": "USER_PROFILE", "condition": "Always", "required_action": "DELETE"},
    "SECURITY_AUDIT_LOG": {"data_category": "SECURITY_AUDIT_LOG",
                           "condition": "Record is within the supplied retention period", "required_action": "RETAIN"},
}


def log(file_id, captured, resource_id, text):
    return record(file_id, captured, "account_service" if resource_id.startswith("account") else "security_service",
                  resource_id, {"raw": text}, text=text)


class LatestObservationTest(unittest.TestCase):
    def test_replica_copy_survives_later_primary_readback(self):
        """case_05: replica row present on 09-02, primary readback 404 on 09-05."""
        replica = record("evidence_54", "2026-09-02T23:00:00Z", "account_service", "account_005",
                         {"resource_id": "account_005", "email": "l@x.com", "account_status": "ACTIVE"},
                         store="customer_profiles_failover_replica")
        primary = record("evidence_90", "2026-09-05T07:18:01Z", "account_service", "account_005",
                         {"observed_state": "NOT_FOUND"})
        chosen = latest_observations([replica, primary], [], ts("2026-09-08T13:40:00Z"))
        self.assertEqual(chosen[("account_service", "account_005")]["file_id"], "evidence_54")

    def test_snapshot_omission_proves_removal_but_never_drops_retained_records(self):
        account = record("evidence_10", "2026-08-01T00:00:00Z", "account_service", "account_001",
                         {"resource_id": "account_001", "account_status": "ACTIVE"})
        audit = record("evidence_10", "2026-08-01T00:00:00Z", "security_service", "audit_record_9",
                       {"resource_id": "audit_record_9", "subject_reference": "user_1"})
        snapshot = coverage_record("evidence_54", ts("2026-09-02T23:00:00Z"),
                                   {("account_service", "primary"), ("security_service", "primary")}, set())
        chosen = latest_observations([account, audit], [snapshot], ts("2026-09-03T11:30:00Z"))
        self.assertEqual(chosen[("account_service", "account_001")]["file_id"], "evidence_54")
        self.assertFalse(record_present(chosen[("account_service", "account_001")]))
        self.assertEqual(chosen[("security_service", "audit_record_9")]["file_id"], "evidence_10")

    def test_snapshot_after_audit_at_is_inadmissible(self):
        account = record("evidence_10", "2026-08-01T00:00:00Z", "account_service", "account_001",
                         {"resource_id": "account_001", "account_status": "ACTIVE"})
        late = coverage_record("evidence_54", ts("2026-09-04T00:00:00Z"), {("account_service", "primary")}, set())
        chosen = latest_observations([account], [late], ts("2026-09-03T11:30:00Z"))
        self.assertTrue(record_present(chosen[("account_service", "account_001")]))


class AdmissibilityTest(unittest.TestCase):
    def test_record_observed_after_audit_at_is_excluded(self):
        """case_135: an admissible file lists account_1417 FOUND with recorded_at after audit_at."""
        base = {"resource_id": "account_1417", "record_type": "USER_PROFILE", "subject_reference": "o@x.com"}
        early = record("evidence_13", "2026-08-12T09:00:00Z", "account_service", "account_1417",
                       dict(base, recorded_at="2026-08-12T09:00:00Z", result="NOT_FOUND"))
        late = record("evidence_13", "2026-08-12T09:00:00Z", "account_service", "account_1417",
                      dict(base, recorded_at="2026-08-14T09:00:00Z", result="FOUND"))
        self.assertEqual(find_relevant_records({"o@x.com"}, ts("2026-08-12T09:00:00Z"), [early, late], []), [early])


class StepOrderingTest(unittest.TestCase):
    def test_steps_are_ordered_and_numbered(self):
        def step(phase, op, system, resource, state):
            return {"phase": phase, "system_id": system, "resource_id": resource,
                    "operation": op, "state": state, "evidence_id": ""}
        steps = build_steps(
            [step("FINDING", "REPORT_EXPOSED_PII", "support_platform", "support_1", "PRESENT"),
             step("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_1", "PRESENT")],
            [step("REMEDIATION", "DELETE_RECORD", "search_service", "search_1", "ABSENT"),
             step("REMEDIATION", "DISABLE_DATA_FLOW", "integration_service", "data_flow_1", "DISABLED")],
            [step("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_1", "PRESENT"),
             step("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1", "ABSENT"),
             step("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1", "ABSENT")],
        )
        self.assertEqual([s["step_no"] for s in steps], list(range(1, 8)))
        self.assertEqual([s["operation"] for s in steps], [
            "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "DISABLE_DATA_FLOW", "DELETE_RECORD",
            "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT"])
        self.assertEqual(steps[4]["system_id"], "account_service")


class AuditCaseTest(unittest.TestCase):
    """End-to-end decisions on a two-person synthetic dataset."""

    def context(self, evidence):
        identities = [{"user_id": "user_1", "email": "a@x.com", "phone": "", "email_hash": ""}]
        return AuditContext(identities=identities, audit_log=[], identity_index=build_identity_index(identities, []),
                            rules=RULES, evidence=evidence, system_activity=[])

    def case(self, text="Please delete my account."):
        return {"case_id": "case_1", "email": "a@x.com", "audit_at": "2026-09-10T00:00:00Z", "request_text": text}

    def test_non_deletion_request(self):
        self.assertEqual(audit_case(self.case("Please send me a copy of my data."), self.context([])),
                         ("case_1", "NOT_APPLICABLE", []))

    def test_complete_when_evidence_proves_removal(self):
        evidence = [log("evidence_1", "2026-09-01T00:00:00Z", "account_9",
                        "lookup_key=a@x.com resource_id=account_9 rows=0 http_status=404"),
                    log("evidence_1", "2026-09-01T00:00:00Z", "audit_record_3",
                        "resource_id=audit_record_3 subject_reference=user_1 rows=1")]
        case_id, status, steps = audit_case(self.case(), self.context(evidence))
        self.assertEqual(status, "COMPLETE")
        self.assertEqual([(s["operation"], s["evidence_id"]) for s in steps],
                         [("VERIFY_RECORD_ABSENT", "evidence_1"), ("VERIFY_RECORD_PRESENT", "evidence_1")])

    def test_incomplete_when_a_copy_remains(self):
        evidence = [log("evidence_1", "2026-09-01T00:00:00Z", "account_9",
                        "lookup_key=a@x.com resource_id=account_9 rows=1 http_status=200")]
        _, status, steps = audit_case(self.case(), self.context(evidence))
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual([(s["phase"], s["operation"], s["evidence_id"]) for s in steps], [
            ("FINDING", "REPORT_REMAINING_RECORD", "evidence_1"),
            ("REMEDIATION", "DELETE_RECORD", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "")])

    def test_retained_audit_record_alone_is_not_complete(self):
        evidence = [log("evidence_1", "2026-09-01T00:00:00Z", "audit_record_3",
                        "resource_id=audit_record_3 subject_reference=user_1 rows=1")]
        self.assertEqual(audit_case(self.case(), self.context(evidence))[1], "INCOMPLETE")

    def test_evidence_after_audit_at_is_ignored(self):
        evidence = [log("evidence_9", "2026-09-20T00:00:00Z", "account_9",
                        "lookup_key=a@x.com resource_id=account_9 rows=1")]
        self.assertEqual(audit_case(self.case(), self.context(evidence)), ("case_1", "INCOMPLETE", []))


if __name__ == "__main__":
    unittest.main()
