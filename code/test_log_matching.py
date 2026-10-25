#!/usr/bin/env python3
"""
Regression test for log record matching.

Tests that log records with only a "raw" field can be matched through
identifiers found in the raw text, not just through explicit fields.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import record_matches_identifiers

def test_log_record_with_raw_field():
    """Log records with only 'raw' field should match identifiers in the raw text."""
    record = {
        "record": {
            "raw": "GET /api/accounts/account_9549 - rows=0, http_status=404, email=tomasz.stojanovic@outlook.com"
        },
        "text": "GET /api/accounts/account_9549 - rows=0, http_status=404, email=tomasz.stojanovic@outlook.com"
    }
    identifiers = ["tomasz.stojanovic@outlook.com", "user_162", "account_9549"]
    
    result = record_matches_identifiers(record, identifiers)
    assert result, "Log record with email in raw text should match"
    print("✓ Log record with email in raw text matches correctly")

def test_log_record_with_user_id():
    """Log records should match user_id found in raw text."""
    record = {
        "record": {
            "raw": "DELETE operation for user_162 completed at 2026-07-15"
        },
        "text": "DELETE operation for user_162 completed at 2026-07-15"
    }
    identifiers = ["user_162", "tomasz.stojanovic@outlook.com"]
    
    result = record_matches_identifiers(record, identifiers)
    assert result, "Log record with user_id in raw text should match"
    print("✓ Log record with user_id in raw text matches correctly")

def test_log_record_with_account_reference():
    """Log records should match account reference IDs with boundary checking."""
    record = {
        "record": {
            "raw": "Resource lookup: account_9549 not found"
        },
        "text": "Resource lookup: account_9549 not found"
    }
    identifiers = ["account_9549", "user_162"]
    
    result = record_matches_identifiers(record, identifiers)
    assert result, "Log record with account reference in raw text should match"
    print("✓ Log record with account reference matches correctly")

def test_log_record_without_identifiers():
    """Log records should not match when identifiers are absent."""
    record = {
        "record": {
            "raw": "System maintenance completed successfully"
        },
        "text": "System maintenance completed successfully"
    }
    identifiers = ["user_999", "test@example.com", "account_123"]
    
    result = record_matches_identifiers(record, identifiers)
    assert not result, "Log record without identifiers should not match"
    print("✓ Log record without identifiers correctly does not match")

def test_structured_record_still_works():
    """Structured records with explicit fields should still work as before."""
    record = {
        "record": {
            "user_id": "user_162",
            "email": "tomasz.stojanovic@outlook.com",
            "action": "profile_update"
        },
        "text": "user_id: user_162, email: tomasz.stojanovic@outlook.com, action: profile_update"
    }
    identifiers = ["user_162", "tomasz.stojanovic@outlook.com"]
    
    result = record_matches_identifiers(record, identifiers)
    assert result, "Structured record with explicit fields should match"
    print("✓ Structured record with explicit fields matches correctly")

def test_structured_record_without_match():
    """Structured records without matching identifiers should not match."""
    record = {
        "record": {
            "user_id": "user_999",
            "email": "other@example.com",
            "action": "login"
        },
        "text": "user_id: user_999, email: other@example.com, action: login"
    }
    identifiers = ["user_162", "tomasz.stojanovic@outlook.com"]
    
    result = record_matches_identifiers(record, identifiers)
    assert not result, "Structured record without matching identifiers should not match"
    print("✓ Structured record without matching identifiers correctly does not match")

def test_log_user_id_prefix_does_not_match():
    """user_11 must not match a log line about user_110 (case_34 false positive)."""
    raw = "service=security_service resource_id=audit_record_2103 subject_reference=user_110 rows=1"
    record = {"record": {"raw": raw}, "text": raw}
    assert not record_matches_identifiers(record, ["user_11", "user_11"]), "user_11 must not match user_110"
    print("✓ user_id prefix collision correctly does not match")


def test_email_suffix_does_not_match():
    """ann@x.com must not match joann@x.com in raw or structured records."""
    raw = "lookup_key=joann@x.com rows=1"
    assert not record_matches_identifiers({"record": {"raw": raw}, "text": raw}, ["ann@x.com"])
    rec = {"record": {"email": "joann@x.com", "user_id": "user_110"}, "text": "{}"}
    assert not record_matches_identifiers(rec, ["ann@x.com", "user_11"])
    print("✓ email/user_id substrings correctly do not match")


def test_case_164_trace_group_absent():
    """Real evidence_85 trace: lookup by email then rows=0/404 -> matched and absent."""
    from main import parse_log_file, record_present
    import tempfile
    lines = (
        "2026-07-28T04:44:49Z level=DEBUG service=account_service trace_id=rb-044449-014 "
        "event=profile_lookup_started resource_id=account_9549 lookup_key=tomasz.stojanovic@outlook.com\n"
        "2026-07-28T04:44:49Z level=INFO service=account_service trace_id=rb-044449-014 "
        "event=profile_lookup_completed resource_id=account_9549 rows=0 http_status=404\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write(lines)
    try:
        recs = parse_log_file(f.name, "evidence_85", None)
    finally:
        os.unlink(f.name)
    assert len(recs) == 1 and recs[0]["resource_id"] == "account_9549"
    assert record_matches_identifiers(recs[0], ["tomasz.stojanovic@outlook.com", "user_162"])
    assert not record_matches_identifiers(recs[0], ["stojanovic@outlook.com", "user_16"])
    assert not record_present(recs[0])
    print("✓ case_164 trace group matched to subject and marked absent")


def test_system_from_table():
    """customer_profiles_* is the account store; only analytics tables map to analytics."""
    from main import system_from_table
    assert system_from_table("customer_profiles_failover_replica") == "account_service"
    assert system_from_table("customer_profiles") == "account_service"
    assert system_from_table("customer_analytics_profiles") == "analytics_platform"
    assert system_from_table("security_audit_records") == "security_service"
    print("✓ system_from_table maps profile vs analytics tables correctly")


if __name__ == "__main__":
    print("Testing log record matching fix...")
    print()
    test_system_from_table()
    test_log_user_id_prefix_does_not_match()
    test_email_suffix_does_not_match()
    test_case_164_trace_group_absent()

    test_log_record_with_raw_field()
    test_log_record_with_user_id()
    test_log_record_with_account_reference()
    test_log_record_without_identifiers()
    test_structured_record_still_works()
    test_structured_record_without_match()
    
    print()
    print("All log record matching tests passed! ✓")
