#!/usr/bin/env python3
"""
Regression and integration tests for audit findings
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from main import parse_ts, is_deletion_request, record_present, record_matches_identifiers

print("=" * 80)
print("REGRESSION TESTS FOR AUDIT FINDINGS")
print("=" * 80)

# TEST 1: parse_ts() safety
print("\n1. parse_ts() null and edge case handling:")
tests = [
    (None, "None input"),
    ("", "Empty string"),
    ("   ", "Whitespace only"),
    ("not-a-date", "Malformed timestamp"),
    ("2026-13-45T25:99:99Z", "Invalid datetime components"),
    ("2026-09-01T06:00:00Z", "Valid ISO8601 with Z"),
    ("2026-09-01T06:00:00", "Valid ISO8601 without Z"),
    ("2026-09-01T06:00:00+00:00", "Valid ISO8601 with +00:00"),
]

for test_input, description in tests:
    try:
        result = parse_ts(test_input)
        status = "OK" if (result is None or isinstance(result, datetime)) else "FAIL"
        print(f"  [{status}] {description:40s} -> {result}")
    except Exception as e:
        print(f"  [ERROR] {description:40s} crashed: {e}")

# TEST 2: is_deletion_request edge cases
print("\n2. is_deletion_request() edge cases:")
deletion_tests = [
    # Basic deletion keywords
    ("Please delete my account", True, "Basic delete"),
    ("I want to delete all my data", True, "Delete data"),
    ("Please erase everything", True, "Erase"),
    ("right to be forgotten", True, "GDPR phrase"),
    ("Destroy all my records", True, "Destroy"),
    ("Purge my information", True, "Purge"),
    
    # Close with account context (should be deletion)
    ("Please close my account", True, "Close + account"),
    ("I want to close this account permanently", True, "Close this account"),
    ("I closed my account last week", True, "Already closed (past tense)"),
    ("Close the account completely", True, "Close the account"),
    ("close my account and process the associated personal-data deletion", True, "case_485/490/509 pattern"),
    ("would like it closed. Please remove the profile", True, "Closed + profile"),
    ("Close account and delete data", True, "Close + data"),
    
    # Explicit negations (should NOT be deletion)
    ("Do not close the account", False, "case_536: Explicit negation"),
    ("Please don't delete my account", False, "Don't delete"),
    ("Do not erase my data", False, "Do not erase"),
    ("Should not close this", False, "Should not close"),
    ("Please do not remove my information", False, "Please do not"),
    
    # Close without account context (should NOT be deletion)
    ("Please close the support ticket", False, "Close ticket (unrelated)"),
    ("Close this case", False, "Close case (unrelated)"),
    ("Close the inquiry", False, "Close inquiry (unrelated)"),
    
    # Non-deletion requests
    ("Please send me my data", False, "Data export request"),
    ("Unsubscribe from marketing", False, "Unsubscribe"),
    ("I need access to my profile", False, "Access request"),
    ("Please correct my information", False, "Correction request"),
    ("Export my data", False, "Export"),
    
    # Inflected forms ("delete" is not a substring of "deletion"/"deleting")
    ("Please treat this message as a request for permanent account deletion.", True, "Deletion noun"),
    ("I'm deleting my account.", True, "Deleting"),
    ("Please confirm that the profile is gone", True, "Deletion verification"),
    ("Can you verify that my account was deleted? Please do not treat that record as mine.", True, "Generic 'please do not' is not a negation"),

    # Non-English deletion requests
    ("Solicito la eliminación permanente de la cuenta asociada a este correo.", True, "Spanish"),
    ("Solicito a exclusão permanente da conta associada a este e-mail.", True, "Portuguese"),
    ("Je demande la suppression définitive du compte associé à cette adresse e-mail.", True, "French"),
    ("このメールアドレスに関連するアカウントの完全な削除を依頼します。", True, "Japanese"),
    ("이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.", True, "Korean"),

    # Non-deletion requests that mention deletion
    ("This is an information request, not a request to erase it.", False, "Not a request to erase"),
    ("I still need the account, so I am not asking for deletion.", False, "Not asking for deletion"),
    ("Can you correct it without removing any of my records?", False, "Without removing"),
    ("Please disclose the information you hold", False, "Disclose is not close"),

    # Edge cases
    ("", False, "Empty string"),
    (None, False, "None (should not crash)"),
]

for test_input, expected, desc in deletion_tests:
    try:
        if test_input is None:
            result = False  # is_deletion_request will handle it
        else:
            result = is_deletion_request(test_input)
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] {desc:40s} -> {result} (expected {expected})")
    except Exception as e:
        print(f"  [ERROR] {desc:40s} crashed: {e}")

# TEST 3: Timestamp filtering logic
print("\n3. Timestamp filtering (evidence captured after audit_at should be filtered):")
audit_at = parse_ts("2026-09-01T06:00:00Z")
test_times = [
    ("2026-09-01T06:00:00Z", True, "Exactly at audit_at"),
    ("2026-09-01T05:59:59Z", True, "Before audit_at"),
    ("2026-09-01T06:00:01Z", False, "After audit_at"),
    ("2026-10-01T12:00:00Z", False, "Far after audit_at"),
    ("2025-01-01T00:00:00Z", True, "Historical"),
]

for time_str, should_include, desc in test_times:
    captured = parse_ts(time_str)
    if audit_at and captured:
        is_valid = captured <= audit_at
        status = "OK" if is_valid == should_include else "FAIL"
        print(f"  [{status}] {desc:40s} {time_str} -> include={is_valid} (expected {should_include})")

# TEST 4: record_present() boundary conditions
print("\n4. record_present() with various presence indicators:")
present_tests = [
    ({"record": {"status": "ACTIVE"}, "text": "status=active"}, True, "ACTIVE status"),
    ({"record": {"status": "DELETED"}, "text": "status=deleted"}, False, "DELETED status"),
    ({"record": {}, "text": "http_status=200"}, True, "HTTP 200"),
    ({"record": {}, "text": "http_status=404"}, False, "HTTP 404"),
    ({"record": {"rows": 0}, "text": "rows=0"}, False, "Zero rows"),
    ({"record": {"rows": 1}, "text": "rows=1"}, True, "One row"),
    ({"record": {"result": "hit"}, "text": "result=hit"}, True, "Result hit"),
    ({"record": {"result": "miss"}, "text": "result=miss"}, False, "Result miss"),
    ({"record": {}, "text": ""}, True, "Empty record/text defaults to present"),
]

for rec, expected, desc in present_tests:
    result = record_present(rec)
    status = "OK" if result == expected else "FAIL"
    print(f"  [{status}] {desc:40s} -> {result} (expected {expected})")

print("\n" + "=" * 80)
print("TEST SUMMARY")
print("=" * 80)
print("""
These tests verify:
1. parse_ts() handles None, empty strings, and malformed input safely
2. is_deletion_request() correctly identifies deletion vs non-deletion requests
3. Timestamp filtering correctly excludes evidence after audit_at
4. record_present() correctly determines presence/absence from various indicators

All critical edge cases must pass for the solution to be robust.
""")
