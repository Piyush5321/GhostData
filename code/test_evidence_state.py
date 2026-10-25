#!/usr/bin/env python3
"""
Regression tests for evidence normalization: historical identifiers, handover
and replica parsing, treated-state detection, and per-store / snapshot absence.
"""

import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import (
    build_identity_index, resolve_subject, parse_text_file, parse_log_file,
    canonical_system, flow_enabled, restore_allowed, identifier_exposure,
    record_present, latest_observations, coverage_record, store_of,
)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def rec(file_id, captured, system, rid, record, store="primary", text=""):
    return {"file_id": file_id, "captured_at": ts(captured), "system": system, "store": store,
            "resource_id": rid, "record": record, "text": text}


def write_tmp(content, suffix):
    f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    f.write(content)
    f.close()
    return f.name


def test_historical_email_resolves_subject():
    """A request from a former address (user_audit_log old_value) resolves to the user and both emails."""
    identities = [{"user_id": "user_12", "email": "minjun.kim@gmail.com", "phone": "+82 1", "email_hash": "abc"}]
    audit_log = [{"user_id": "user_12", "field_name": "email",
                  "old_value": "minjun.kim90@gmail.com", "new_value": "minjun.kim@gmail.com"}]
    idx, _ = build_identity_index(identities, audit_log)
    user_id, idents = resolve_subject("minjun.kim90@gmail.com", idx, audit_log, identities)
    assert user_id == "user_12"
    assert {"minjun.kim90@gmail.com", "minjun.kim@gmail.com", "user_12", "abc"} <= idents
    print("✓ historical email resolves to the stable user and current identifiers")


def test_handover_rows_parsed_with_state():
    path = write_tmp(
        "CUSTOMER PLATFORM - SHIFT HANDOVER\n"
        "- 2026-10-01T12:00:00Z | support_platform | support_4218 | SUPPORT_ATTACHMENT | FOUND | noor-khalil@proton.me\n"
        "- 2026-10-01T12:00:00Z | integration_service | data_flow_2901 | DATA_FLOW | DISABLED | user_39\n"
        "- 2026-10-01T12:00:00Z | analytics_platform | analytics_6830 | ANALYTICS_PROFILE | ANONYMIZED | no direct identifier\n",
        ".txt")
    try:
        recs = {r["resource_id"]: r for r in parse_text_file(path, "evidence_03", ts("2026-10-01T12:00:00Z"))}
    finally:
        os.unlink(path)
    assert set(recs) == {"support_4218", "data_flow_2901", "analytics_6830"}
    assert record_present(recs["support_4218"]) and identifier_exposure(recs["support_4218"])
    assert flow_enabled(recs["data_flow_2901"]) is False
    assert identifier_exposure(recs["analytics_6830"]) is False
    print("✓ handover rows parsed into resource, category and state")


def test_bracket_log_layout_and_replica_store():
    path = write_tmp(
        '2026-07-28T05:57:10Z [DEBUG] [account_service] [tr-6037447] profile_lookup_started: lookup_key="a@x.com"\n'
        '2026-07-28T05:57:13Z [INFO] [account_service] [tr-6037447] profile_lookup_completed: resource_id="account_9549" rows="0" http_status="404"\n'
        "2026-09-17T22:00:05Z level=INFO service=support_replica trace_id=tr-rp-002 event=contact_query_completed resource_id=support_002 rows=1\n",
        ".log")
    try:
        recs = {r["resource_id"]: r for r in parse_log_file(path, "evidence_x", None)}
    finally:
        os.unlink(path)
    assert recs["account_9549"]["system"] == "account_service"
    assert not record_present(recs["account_9549"]), "quoted rows=\"0\"/404 means absent"
    assert canonical_system("support_002", "support_replica") == "support_platform"
    assert store_of(recs["support_002"]["system"]) == "support_replica"
    print("✓ bracket log layout parsed; replica service kept as its own store")


def test_sql_flags_and_cleared_fields():
    backup = {"record": {"resource_id": "backup_4978", "restore_allowed": "1"}, "text": ""}
    blocked = {"record": {"resource_id": "backup_3120", "restore_allowed": False}, "text": ""}
    flow = {"record": {"resource_id": "data_flow_6244", "enabled": 0}, "text": ""}
    unknown = {"record": {"raw": "service=x resource_id=support_002 rows=1"}, "text": "service=x resource_id=support_002 rows=1"}
    cleared = {"record": {"resource_id": "support_4715", "customer_reference": "user_26",
                          "requester_email": None, "requester_name": None}, "text": ""}
    assert restore_allowed(backup) is True and restore_allowed(blocked) is False
    assert flow_enabled(flow) is False
    assert identifier_exposure(cleared) is False, "stable reference only -> treated"
    assert identifier_exposure(unknown) is None, "no identifier fields visible -> cannot verify treatment"
    print("✓ SQL 0/1 flags and cleared identifier fields detected")


def test_replica_copy_survives_later_primary_readback():
    """case_05 shape: replica still holds the row even though the primary later reads 404."""
    replica = rec("evidence_54", "2026-09-02T23:00:00Z", "account_service", "account_005",
                  {"resource_id": "account_005", "email": "l@x.com", "account_status": "ACTIVE"},
                  store="customer_profiles_failover_replica")
    primary = rec("evidence_90", "2026-09-05T07:18:01Z", "account_service", "account_005",
                  {"observed_state": "NOT_FOUND"})
    chosen = latest_observations([replica, primary], [], ts("2026-09-08T13:40:00Z"))
    assert chosen[("account_service", "account_005")]["file_id"] == "evidence_54"
    print("✓ replica copy is not hidden by a later primary readback")


def test_snapshot_omission_proves_removal_but_not_retained_loss():
    account = rec("evidence_10", "2026-08-01T00:00:00Z", "account_service", "account_001",
                  {"resource_id": "account_001", "email": "a@x.com", "account_status": "ACTIVE"})
    audit = rec("evidence_10", "2026-08-01T00:00:00Z", "security_service", "audit_record_9",
                {"resource_id": "audit_record_9", "subject_reference": "user_1"})
    snap = coverage_record("evidence_54", ts("2026-09-02T23:00:00Z"),
                           {("account_service", "primary"), ("security_service", "primary")}, {"account_8741"})
    chosen = latest_observations([account, audit], [snap], ts("2026-09-03T11:30:00Z"))
    acc = chosen[("account_service", "account_001")]
    assert acc["file_id"] == "evidence_54" and not record_present(acc)
    assert chosen[("security_service", "audit_record_9")]["file_id"] == "evidence_10"
    # A snapshot captured after audit_at is not admissible
    late = latest_observations([account], [dict(snap, captured_at=ts("2026-09-04T00:00:00Z"))],
                               ts("2026-09-03T11:30:00Z"))
    assert record_present(late[("account_service", "account_001")])
    print("✓ snapshot omission proves removal, respects audit_at, never drops retained records")


def test_lookup_found_not_found_json_and_log():
    """evidence_13 JSON and evidence_08/41 log lookups: NOT_FOUND is absent, FOUND is present."""
    from main import category_for_record
    not_found = {"record": {"recorded_at": "2026-08-12T09:00:00Z", "system": "account_service",
                            "resource_id": "account_9191", "record_type": "USER_PROFILE",
                            "result": "NOT_FOUND", "subject_reference": "aarav.m2118@zoho.com"},
                 "text": "", "system": "account_service", "resource_id": "account_9191"}
    found = {"record": dict(not_found["record"], resource_id="account_907", result="FOUND"),
             "text": "", "system": "account_service", "resource_id": "account_907"}
    assert record_present(not_found) is False
    assert record_present(found) is True
    assert category_for_record(not_found, []) == "USER_PROFILE"
    for line, expected in (
        ('2026-03-15T07:00:00Z INFO service=downstream_delivery_service event=record_lookup seq=1 system="account_service" '
         'resource_id="account_022" record_type="USER_PROFILE" result="NOT_FOUND" subject_reference="zofia-novak@gmail.com"', False),
        ('2026-08-31T07:00:00Z INFO service=search_index_service event=record_lookup seq=1 system="account_service" '
         'resource_id="account_8473" record_type="USER_PROFILE" result="FOUND" subject_reference="marek.zielinski@gmail.com"', True),
    ):
        assert record_present({"record": {"raw": line}, "text": line}) is expected, line
    print("✓ FOUND / NOT_FOUND lookups read consistently from JSON and logs")


def test_cache_miss_and_5xx_do_not_decide_presence():
    trace = (
        'ts [DEBUG] [support_platform] [tr-2053433] message_scan_started: subject_key="a@x.com"\n'
        'ts [DEBUG] [support_platform] [tr-2053433] cache_lookup: result="miss" cache="support_platform_lookup_v2"\n'
        'ts [WARN] [support_platform] [tr-2053433] message_pii_scan: resource_id="support_1" rows="1" matched_value="a@x.com"\n'
    )
    assert record_present({"record": {"raw": trace}, "text": trace}) is True, "cache miss must not mean absent"
    gone = 'ts [INFO] [account_service] [tr-1] profile_lookup_completed: resource_id="account_7" rows="0" http_status="404"'
    assert record_present({"record": {"raw": gone}, "text": gone}) is False
    err = "ts level=ERROR service=account_service trace_id=t resource_id=account_8 http_status=503"
    from main import text_presence
    assert text_presence(err) is None, "5xx is ambiguous, not proof of absence"
    print("✓ cache misses and 5xx never decide presence; explicit 404/rows=0 do")


def parse_lines(text):
    path = write_tmp(text, ".log")
    try:
        return parse_log_file(path, "evidence_x", None)
    finally:
        os.unlink(path)


def test_four_missed_log_layouts_grouped_by_trace():
    """requestId/message= (evidence_25/53), JSON-lines (evidence_06/34), pipe and syslog layouts:
    the lookup-by-email line joins its trace's resource line."""
    layouts = {
        "requestId": (
            '2026-06-08T09:50:56Z requestId=tr-0305307 level=DEBUG function=support_platform message="instance=\\"support-5\\" '
            'event=\\"message_scan_started\\" subject_key=\\"aarav-singh@proton.me\\" attempt=\\"1\\""\n'
            '2026-06-08T09:51:04Z requestId=tr-0305307 level=WARN function=support_platform message="instance=\\"support-5\\" '
            'resource_id=\\"support_2498\\" event=\\"message_pii_scan\\" matched_value=\\"aarav-singh@proton.me\\""\n'),
        "json-lines": (
            '2026-09-10T08:56:17Z {"severity":"DEBUG","service":"account_service","trace_id":"tr-5563868",'
            '"event":"profile_lookup_started","lookup_key":"collins.grace@icloud.com"}\n'
            '2026-09-10T08:56:20Z {"severity":"DEBUG","service":"account_service","trace_id":"tr-5563868",'
            '"resource_id":"account_6633","event":"profile_lookup_completed","rows":"0","http_status":"404"}\n'),
        "pipe": (
            '2026-07-03T07:51:30Z | DEBUG | account_service | tr-1 | profile_lookup_started | lookup_key="p@x.com"\n'
            '2026-07-03T07:51:32Z | INFO | account_service | tr-1 | profile_lookup_completed | resource_id="account_5" rows="1" http_status="200"\n'),
        "syslog": (
            '2026-08-22T10:51:11Z <15>1 node-1 account_service tr-4973247 - - level=DEBUG event="profile_lookup_started" lookup_key="nana.b9576@outlook.com"\n'
            '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-4973247 - - level=DEBUG resource_id="account_1785" rows="1" http_status="200"\n'),
    }
    expected = {"requestId": ("support_2498", "support_platform", True, "aarav-singh@proton.me"),
                "json-lines": ("account_6633", "account_service", False, "collins.grace@icloud.com"),
                "pipe": ("account_5", "account_service", True, "p@x.com"),
                "syslog": ("account_1785", "account_service", True, "nana.b9576@outlook.com")}
    from main import record_matches_identifiers
    for name, text in layouts.items():
        recs = parse_lines(text)
        assert len(recs) == 1, (name, recs)
        rid, system, present, email = expected[name]
        r = recs[0]
        assert (r["resource_id"], r["system"], record_present(r)) == (rid, system, present), (name, r)
        assert record_matches_identifiers(r, {email}), name
    print("✓ requestId, JSON-lines, pipe and syslog layouts parsed and grouped by trace")


def test_trace_with_two_resources_is_split():
    recs = parse_lines(
        "ts service=account_service trace_id=t1 event=lookup lookup_key=a@x.com\n"
        "ts service=account_service trace_id=t1 resource_id=account_1 rows=0 http_status=404\n"
        "ts service=search_service trace_id=t1 resource_id=search_2 result=hit\n")
    by_rid = {r["resource_id"]: r for r in recs}
    assert set(by_rid) == {"account_1", "search_2"}
    assert not record_present(by_rid["account_1"]) and record_present(by_rid["search_2"])
    assert "a@x.com" in by_rid["search_2"]["text"] and "search_2" not in by_rid["account_1"]["text"]
    print("✓ a trace naming two resources yields separate records with their own results")


def test_snapshot_scope_limits_coverage():
    """evidence_90 shape: scope lists account_service only, other typed tables are empty."""
    from main import parse_sql_file
    sql = (
        "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);\n"
        "INSERT INTO snapshot_scope VALUES ('account_service', '2026-09-05T07:18:01Z');\n"
        "CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, email TEXT);\n"
        "CREATE TABLE customer_search_documents (resource_id TEXT PRIMARY KEY, email TEXT);\n"
        "CREATE TABLE security_audit_records (resource_id TEXT PRIMARY KEY, subject_reference TEXT);\n"
    )
    path = write_tmp(sql, ".sql")
    try:
        cov = [r for r in parse_sql_file(path, "evidence_90", ts("2026-09-05T07:18:01Z")) if r.get("kind") == "coverage"]
    finally:
        os.unlink(path)
    assert len(cov) == 1 and cov[0]["stores"] == {("account_service", "primary")}, cov
    # Without a scope table, an empty typed table still covers its own store
    path = write_tmp("CREATE TABLE customer_profiles_failover_replica (resource_id TEXT PRIMARY KEY, email TEXT);\n", ".sql")
    try:
        cov = [r for r in parse_sql_file(path, "evidence_54", None) if r.get("kind") == "coverage"]
    finally:
        os.unlink(path)
    assert cov[0]["stores"] == {("account_service", "customer_profiles_failover_replica")}
    print("✓ snapshot_scope limits coverage; unscoped empty tables cover only their own store")


def test_record_observed_after_audit_at_is_excluded():
    """case_135 shape: evidence_13 (captured 08-12) also lists account_1417 FOUND with recorded_at 08-14."""
    from main import find_relevant_records
    base = {"system": "account_service", "resource_id": "account_1417", "record_type": "USER_PROFILE",
            "subject_reference": "o.morgan56@gmail.com"}
    early = rec("evidence_13", "2026-08-12T09:00:00Z", "account_service", "account_1417",
                dict(base, recorded_at="2026-08-12T09:00:00Z", result="NOT_FOUND"))
    late = rec("evidence_13", "2026-08-12T09:00:00Z", "account_service", "account_1417",
               dict(base, recorded_at="2026-08-14T09:00:00Z", result="FOUND"))
    relevant = find_relevant_records("o.morgan56@gmail.com", {"o.morgan56@gmail.com"},
                                     ts("2026-08-12T09:00:00Z"), [early, late], [])
    assert relevant == [early]
    print("✓ a record observed after audit_at is excluded even inside an admissible file")


if __name__ == "__main__":
    test_record_observed_after_audit_at_is_excluded()
    test_snapshot_scope_limits_coverage()
    test_four_missed_log_layouts_grouped_by_trace()
    test_trace_with_two_resources_is_split()
    test_lookup_found_not_found_json_and_log()
    test_cache_miss_and_5xx_do_not_decide_presence()
    test_historical_email_resolves_subject()
    test_handover_rows_parsed_with_state()
    test_bracket_log_layout_and_replica_store()
    test_sql_flags_and_cleared_fields()
    test_replica_copy_survives_later_primary_readback()
    test_snapshot_omission_proves_removal_but_not_retained_loss()
    print()
    print("All evidence state tests passed! ✓")
