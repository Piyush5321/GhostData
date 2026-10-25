import unittest

from tests.helpers import temp_file, ts
from ghostaudit.evidence import (
    canonical_system, catalog_labels, parse_log_file, parse_sql_file, parse_text_file,
    store_of, system_from_table,
)
from ghostaudit.identity import record_matches_identifiers
from ghostaudit.state import flow_enabled, identifier_exposure, record_present


def parse_log(text):
    with temp_file(text, ".log") as path:
        return parse_log_file(path, "evidence_x", None)


class SystemMappingTest(unittest.TestCase):
    def test_resource_prefix_names_the_system(self):
        self.assertEqual(canonical_system("support_002", "support_replica"), "support_platform")
        self.assertEqual(canonical_system("object_19", "object_storage"), "object_storage")

    def test_replicas_are_separate_stores(self):
        self.assertEqual(store_of("support_replica"), "support_replica")
        self.assertEqual(store_of("customer_profiles"), "primary")

    def test_profile_tables(self):
        self.assertEqual(system_from_table("customer_profiles_failover_replica"), "account_service")
        self.assertEqual(system_from_table("customer_analytics_profiles"), "analytics_platform")
        self.assertEqual(system_from_table("security_audit_records"), "security_service")


class LogParsingTest(unittest.TestCase):
    LAYOUTS = {
        "key=value (evidence_85)": (
            "2026-07-28T04:44:49Z level=DEBUG service=account_service trace_id=rb-1 "
            "event=profile_lookup_started resource_id=account_9549 lookup_key=tomasz.stojanovic@outlook.com\n"
            "2026-07-28T04:44:49Z level=INFO service=account_service trace_id=rb-1 "
            "event=profile_lookup_completed resource_id=account_9549 rows=0 http_status=404\n",
            ("account_9549", "account_service", False, "tomasz.stojanovic@outlook.com")),
        "bracket": (
            '2026-07-28T05:57:10Z [DEBUG] [account_service] [tr-6037447] profile_lookup_started: lookup_key="a@x.com"\n'
            '2026-07-28T05:57:13Z [INFO] [account_service] [tr-6037447] profile_lookup_completed: '
            'resource_id="account_9549" rows="0" http_status="404"\n',
            ("account_9549", "account_service", False, "a@x.com")),
        "requestId / message= (evidence_25)": (
            '2026-06-08T09:50:56Z requestId=tr-0305307 level=DEBUG function=support_platform message="'
            'event=\\"message_scan_started\\" subject_key=\\"aarav-singh@proton.me\\""\n'
            '2026-06-08T09:51:04Z requestId=tr-0305307 level=WARN function=support_platform message="'
            'resource_id=\\"support_2498\\" event=\\"message_pii_scan\\" matched_value=\\"aarav-singh@proton.me\\""\n',
            ("support_2498", "support_platform", True, "aarav-singh@proton.me")),
        "JSON-lines (evidence_06)": (
            '2026-09-10T08:56:17Z {"service":"account_service","trace_id":"tr-5563868",'
            '"event":"profile_lookup_started","lookup_key":"collins.grace@icloud.com"}\n'
            '2026-09-10T08:56:20Z {"service":"account_service","trace_id":"tr-5563868",'
            '"resource_id":"account_6633","rows":"0","http_status":"404"}\n',
            ("account_6633", "account_service", False, "collins.grace@icloud.com")),
        "pipe": (
            '2026-07-03T07:51:30Z | DEBUG | account_service | tr-1 | profile_lookup_started | lookup_key="p@x.com"\n'
            '2026-07-03T07:51:32Z | INFO | account_service | tr-1 | profile_lookup_completed | '
            'resource_id="account_5" rows="1" http_status="200"\n',
            ("account_5", "account_service", True, "p@x.com")),
        "RFC 5424 syslog": (
            '2026-08-22T10:51:11Z <15>1 node-1 account_service tr-4973247 - - lookup_key="nana.b9576@outlook.com"\n'
            '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-4973247 - - resource_id="account_1785" '
            'rows="1" http_status="200"\n',
            ("account_1785", "account_service", True, "nana.b9576@outlook.com")),
    }

    def test_layouts_join_lookup_line_to_resource_line_by_trace(self):
        for name, (text, (resource_id, system, present, email)) in self.LAYOUTS.items():
            with self.subTest(layout=name):
                records = parse_log(text)
                self.assertEqual(len(records), 1)
                rec = records[0]
                self.assertEqual((rec["resource_id"], rec["system"], record_present(rec)),
                                 (resource_id, system, present))
                self.assertTrue(record_matches_identifiers(rec, {email}))

    def test_trace_naming_two_resources_is_split(self):
        records = parse_log(
            "ts service=account_service trace_id=t1 lookup_key=a@x.com\n"
            "ts service=account_service trace_id=t1 resource_id=account_1 rows=0 http_status=404\n"
            "ts service=search_service trace_id=t1 resource_id=search_2 result=hit\n")
        by_id = {r["resource_id"]: r for r in records}
        self.assertEqual(set(by_id), {"account_1", "search_2"})
        self.assertFalse(record_present(by_id["account_1"]))
        self.assertTrue(record_present(by_id["search_2"]))
        self.assertIn("a@x.com", by_id["search_2"]["text"])
        self.assertNotIn("search_2", by_id["account_1"]["text"])


class HandoverParsingTest(unittest.TestCase):
    def test_rows_carry_category_state_and_identifier(self):
        text = ("CUSTOMER PLATFORM - SHIFT HANDOVER\n"
                "- 2026-10-01T12:00:00Z | support_platform | support_4218 | SUPPORT_ATTACHMENT | FOUND | noor-khalil@proton.me\n"
                "- 2026-10-01T12:00:00Z | integration_service | data_flow_2901 | DATA_FLOW | DISABLED | user_39\n"
                "- 2026-10-01T12:00:00Z | analytics_platform | analytics_6830 | ANALYTICS_PROFILE | ANONYMIZED | no direct identifier\n")
        with temp_file(text, ".txt") as path:
            records = {r["resource_id"]: r for r in parse_text_file(path, "evidence_03", ts("2026-10-01T12:00:00Z"))}
        self.assertEqual(set(records), {"support_4218", "data_flow_2901", "analytics_6830"})
        self.assertTrue(identifier_exposure(records["support_4218"]))
        self.assertFalse(flow_enabled(records["data_flow_2901"]))
        self.assertFalse(identifier_exposure(records["analytics_6830"]))


class SqlSnapshotTest(unittest.TestCase):
    def coverage(self, sql):
        with temp_file(sql, ".sql") as path:
            return [r for r in parse_sql_file(path, "evidence_x", None) if r.get("kind") == "coverage"]

    def test_snapshot_scope_limits_coverage(self):
        coverage = self.coverage(
            "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT);\n"
            "INSERT INTO snapshot_scope VALUES ('account_service', '2026-09-05T07:18:01Z');\n"
            "CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, email TEXT);\n"
            "CREATE TABLE customer_search_documents (resource_id TEXT PRIMARY KEY, email TEXT);\n")
        self.assertEqual(coverage[0]["stores"], {("account_service", "primary")})

    def test_unscoped_empty_table_covers_only_its_own_store(self):
        coverage = self.coverage("CREATE TABLE customer_profiles_failover_replica (resource_id TEXT, email TEXT);\n")
        self.assertEqual(coverage[0]["stores"], {("account_service", "customer_profiles_failover_replica")})


class CatalogLabelTest(unittest.TestCase):
    def test_business_labels_map_to_resources(self):
        activity = [
            {"event_type": "LINK_RESOURCE_IDENTITY",
             "details": "Data catalog linked export_1842 (NST-2026-0718-1842) to ingrid.dahl@outlook.com and user_18."},
            {"event_type": "LINK_RESOURCE_IDENTITY",
             "details": "Data catalog linked search_428 (search_428) to g.silva76@gmail.com and user_13."},
            {"event_type": "LINK_RESOURCE_IDENTITY",
             "details": "Data catalog linked tombstone_17 (user_17) to leonie.g9352@outlook.com and user_17."},
            {"event_type": "DELETE_RECORD", "details": "Deletion worker submitted export_9 (LABEL-9)."},
        ]
        self.assertEqual(catalog_labels(activity), {"NST-2026-0718-1842": "export_1842"})


if __name__ == "__main__":
    unittest.main()
