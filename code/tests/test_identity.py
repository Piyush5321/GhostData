import unittest

from tests.helpers import log_record
from ghostaudit.identity import (
    build_identity_index, contains_identifier, record_matches_identifiers, resolve_subject,
)


class IdentityResolutionTest(unittest.TestCase):
    def setUp(self):
        self.identities = [{"user_id": "user_12", "email": "minjun.kim@gmail.com",
                            "phone": "+82 10 2466 4678", "email_hash": "abc123"}]
        self.audit_log = [{"user_id": "user_12", "field_name": "email",
                           "old_value": "minjun.kim90@gmail.com", "new_value": "minjun.kim@gmail.com"}]
        self.index = build_identity_index(self.identities, self.audit_log)

    def test_historical_email_resolves_to_stable_user(self):
        user_id, identifiers = resolve_subject("Minjun.Kim90@gmail.com", self.index,
                                               self.identities, self.audit_log)
        self.assertEqual(user_id, "user_12")
        self.assertLessEqual({"minjun.kim90@gmail.com", "minjun.kim@gmail.com", "user_12",
                              "+82 10 2466 4678", "abc123"}, identifiers)

    def test_unknown_email_keeps_only_itself(self):
        self.assertEqual(resolve_subject("nobody@x.com", self.index, self.identities, self.audit_log),
                         (None, {"nobody@x.com"}))


class IdentifierMatchingTest(unittest.TestCase):
    def test_whole_token_matching(self):
        self.assertTrue(contains_identifier("subject_reference=user_11 rows=1", "user_11"))
        self.assertFalse(contains_identifier("subject_reference=user_110", "user_11"))
        self.assertFalse(contains_identifier("lookup_key=joann@x.com", "ann@x.com"))

    def test_log_record_matches_on_text(self):
        line = ("2026-07-28T04:44:49Z service=account_service trace_id=rb-1 resource_id=account_9549 "
                "lookup_key=tomasz.stojanovic@outlook.com")
        self.assertTrue(record_matches_identifiers(log_record(line), {"tomasz.stojanovic@outlook.com"}))
        self.assertFalse(record_matches_identifiers(log_record(line), {"stojanovic@outlook.com", "user_16"}))

    def test_structured_record_matches_any_field_including_json_payloads(self):
        rec = {"record": {"resource_id": "search_1007",
                          "payload": '{"document": {"email": "diya.k4436@outlook.com"}}'},
               "text": "", "system": "search_service", "resource_id": "search_1007"}
        self.assertTrue(record_matches_identifiers(rec, {"diya.k4436@outlook.com"}))
        other = {"record": {"user_id": "user_110", "email": "joann@x.com"}, "text": "",
                 "system": "account_service", "resource_id": "account_1"}
        self.assertFalse(record_matches_identifiers(other, {"user_11", "ann@x.com"}))


if __name__ == "__main__":
    unittest.main()
