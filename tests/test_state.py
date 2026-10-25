import unittest

from tests.helpers import log_record
from ghostaudit.policy import category_for_record
from ghostaudit.state import (
    flow_enabled, identifier_exposure, record_present, restore_allowed, text_presence,
)


def structured(content):
    return {"record": content, "text": "", "system": "unknown_service",
            "resource_id": content.get("resource_id", "")}


class PresenceTest(unittest.TestCase):
    def test_lookup_results_in_json(self):
        """evidence_13: NOT_FOUND is absent, FOUND is present, record_type gives the category."""
        base = {"resource_id": "account_9191", "record_type": "USER_PROFILE",
                "subject_reference": "aarav.m2118@zoho.com"}
        not_found = structured(dict(base, result="NOT_FOUND"))
        self.assertFalse(record_present(not_found))
        self.assertTrue(record_present(structured(dict(base, result="FOUND"))))
        self.assertEqual(category_for_record(not_found), "USER_PROFILE")

    def test_lookup_results_in_logs(self):
        """evidence_08 / evidence_41 record_lookup lines."""
        line = ('2026-03-15T07:00:00Z INFO service=downstream_delivery_service event=record_lookup '
                'system="account_service" resource_id="account_022" record_type="USER_PROFILE" '
                'result="{}" subject_reference="zofia-novak@gmail.com"')
        self.assertFalse(record_present(log_record(line.format("NOT_FOUND"))))
        self.assertTrue(record_present(log_record(line.format("FOUND"))))

    def test_cache_miss_does_not_mean_absent(self):
        trace = ('[support_platform] [tr-1] cache_lookup: result="miss" cache="support_platform_lookup_v2"\n'
                 '[support_platform] [tr-1] message_pii_scan: resource_id="support_1" rows="1"\n')
        self.assertTrue(record_present(log_record(trace)))

    def test_explicit_absence_and_ambiguous_errors(self):
        self.assertFalse(record_present(log_record('resource_id="account_7" rows="0" http_status="404"')))
        self.assertIsNone(text_presence("service=account_service resource_id=account_8 http_status=503"))

    def test_structured_absence_signals(self):
        self.assertFalse(record_present(structured({"resource_id": "account_1", "status_code": 404, "profile": None})))
        self.assertFalse(record_present(structured({"resource_id": "account_2", "account_status": "DELETED"})))
        self.assertTrue(record_present(structured({"resource_id": "account_3", "account_status": "ACTIVE"})))

    def test_missing_evidence_is_never_proof_of_deletion(self):
        self.assertTrue(record_present(structured({"resource_id": "support_9"})))


class FlagsAndExposureTest(unittest.TestCase):
    def test_flags_from_sql_and_json(self):
        self.assertTrue(restore_allowed(structured({"resource_id": "backup_4978", "restore_allowed": "1"})))
        self.assertFalse(restore_allowed(structured({"resource_id": "backup_3120", "restore_allowed": False})))
        self.assertFalse(flow_enabled(structured({"resource_id": "data_flow_6244", "enabled": 0})))
        self.assertIsNone(flow_enabled(structured({"resource_id": "data_flow_1"})))

    def test_identifier_exposure_is_three_valued(self):
        cleared = structured({"resource_id": "support_4715", "customer_reference": "user_26",
                              "requester_email": None, "requester_name": None})
        exposed = structured({"resource_id": "support_5569", "requester_email": "mori.ren@gmail.com"})
        unseen = log_record("service=support_replica resource_id=support_002 rows=1")
        self.assertFalse(identifier_exposure(cleared))
        self.assertTrue(identifier_exposure(exposed))
        self.assertIsNone(identifier_exposure(unseen))


if __name__ == "__main__":
    unittest.main()
