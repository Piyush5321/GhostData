import unittest

from tests.helpers import record, ts
from ghostaudit.audit import AuditContext, audit_case
from ghostaudit.evidence import coverage_record
from ghostaudit.identity import build_identity_index
from ghostaudit.linkage import activity_links

IDENTITIES = [
    {"user_id": "user_1", "email": "a@x.com", "phone": "", "email_hash": ""},
    {"user_id": "user_2", "email": "b@x.com", "phone": "", "email_hash": ""},
]


def event(time, event_type, target, resource_id, result):
    return {"event_time": time, "event_type": event_type, "source_system": "deletion_worker",
            "target_system": target, "resource_id": resource_id, "reported_result": result}


def request(case_id, email, status, reported_at="2026-09-02T12:00:00Z", audit_at="2026-09-15T12:00:00Z"):
    return {"case_id": case_id, "email": email, "reported_status": status, "reported_at": reported_at,
            "audit_at": audit_at, "requested_at": "2026-08-30T12:00:00Z",
            "request_text": "Please delete my account."}


def context(activity, evidence=()):
    return AuditContext(identities=IDENTITIES, audit_log=[], identity_index=build_identity_index(IDENTITIES, []),
                        rules={}, evidence=list(evidence), system_activity=activity)


class ActivityLinkTest(unittest.TestCase):
    def test_reported_status_disambiguates_account_events(self):
        """case_24 / case_25 shape: same reported_at, one COMPLETE (SUCCESS) and one NOT_STARTED (FAILED)."""
        activity = [event("2026-09-02T10:48:27Z", "DELETE_RECORD", "account_service", "account_3136", "FAILED"),
                    event("2026-09-02T10:59:41Z", "DELETE_RECORD", "account_service", "account_5334", "SUCCESS")]
        cases = [request("case_a", "a@x.com", "NOT_STARTED"), request("case_b", "b@x.com", "COMPLETE")]
        self.assertEqual(activity_links(cases, context(activity)),
                         {"case_a": {"account_3136"}, "case_b": {"account_5334"}})

    def test_ambiguous_candidates_are_not_linked(self):
        activity = [event("2026-09-02T11:31:08Z", "ANONYMIZE_RECORD", "analytics_platform", "analytics_1", "SUCCESS"),
                    event("2026-09-02T11:31:24Z", "ANONYMIZE_RECORD", "analytics_platform", "analytics_2", "SUCCESS")]
        cases = [request("case_a", "a@x.com", "NOT_STARTED"), request("case_b", "b@x.com", "NOT_STARTED")]
        self.assertEqual(activity_links(cases, context(activity)), {})

    def test_resource_owned_by_someone_else_is_excluded(self):
        activity = [event("2026-09-02T11:00:00Z", "DELETE_RECORD", "search_service", "search_9", "FAILED")]
        evidence = [record("evidence_1", "2026-09-01T00:00:00Z", "search_service", "search_9",
                           {"resource_id": "search_9", "email": "b@x.com"})]
        links = activity_links([request("case_a", "a@x.com", "NOT_STARTED")], context(activity, evidence))
        self.assertEqual(links, {})

    def test_events_outside_the_window_or_after_audit_at_are_ignored(self):
        activity = [event("2026-09-01T12:00:00Z", "DELETE_RECORD", "account_service", "account_1", "SUCCESS"),
                    event("2026-09-02T12:10:00Z", "DELETE_RECORD", "search_service", "search_1", "SUCCESS")]
        late_audit = request("case_a", "a@x.com", "COMPLETE", audit_at="2026-09-02T12:05:00Z")
        self.assertEqual(activity_links([late_audit], context(activity)), {})


class LinkedResourceStateTest(unittest.TestCase):
    def test_linked_account_absent_from_covering_snapshot_is_verified_absent(self):
        """case_01 shape: account_001 is named only by the deletion worker and missing from evidence_54."""
        snapshot = coverage_record("evidence_54", ts("2026-09-02T23:00:00Z"), {("account_service", "primary")},
                                   {"account_8741"})
        ctx = context([], [snapshot])
        ctx.activity_links = {"case_a": {"account_001"}}
        _, status, steps = audit_case(request("case_a", "a@x.com", "COMPLETE"), ctx)
        self.assertEqual(status, "COMPLETE")
        self.assertEqual([(s["operation"], s["resource_id"], s["evidence_id"]) for s in steps],
                         [("VERIFY_RECORD_ABSENT", "account_001", "evidence_54")])


if __name__ == "__main__":
    unittest.main()
