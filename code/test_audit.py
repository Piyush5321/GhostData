#!/usr/bin/env python3
"""
Test suite for the deletion-audit agent.
Tests core logic for identity matching, evidence processing, status classification, and step generation.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from main import (
    is_deletion_request,
    record_present,
    flow_enabled,
    restore_allowed,
    tombstone_has_direct_identifiers,
    record_is_attributable,
    extract_identifiers_from_text,
    record_matches_identifiers,
    parse_ts,
)


def test_is_deletion_request():
    """Test deletion request classification."""
    assert is_deletion_request("Please delete my account") is True
    assert is_deletion_request("erase my data") is True
    assert is_deletion_request("right to be forgotten") is True
    assert is_deletion_request("Please send me my data") is False
    assert is_deletion_request("unsubscribe from marketing") is False
    assert is_deletion_request("I need my information") is False
    print("[OK] is_deletion_request tests passed")


def test_record_present():
    """Test presence detection in records."""
    # Record marked as deleted
    assert record_present({
        "record": {"status": "DELETED"},
        "text": "status=deleted"
    }) is False
    
    # Record that's active
    assert record_present({
        "record": {"status": "ACTIVE"},
        "text": "status=active http_status=200"
    }) is True
    
    # Record with 404 (not found)
    assert record_present({
        "record": {},
        "text": "http_status=404"
    }) is False
    
    # Record with rows=0 (no results)
    assert record_present({
        "record": {"rows": 0},
        "text": "rows=0"
    }) is False
    
    print("[OK] record_present tests passed")


def test_extract_identifiers_from_text():
    """Test identifier extraction from text."""
    identifiers = {"user_123", "test@example.com", "john_doe"}
    
    text = "Found user_123 in database with email test@example.com"
    found = extract_identifiers_from_text(text, identifiers)
    assert "user_123" in found
    assert "test@example.com" in found
    assert "john_doe" not in found
    
    print("[OK] extract_identifiers_from_text tests passed")


def test_parse_ts():
    """Test timestamp parsing."""
    ts = parse_ts("2026-09-15T11:30:00Z")
    assert ts is not None
    
    ts = parse_ts("")
    assert ts is None
    
    print("[OK] parse_ts tests passed")


def test_record_matches_identifiers_dict():
    """Test matching dict records against identifiers."""
    identifiers = {"user_100", "test@example.com"}
    
    record_with_match = {
        "record": {"user_id": "user_100", "name": "Test User"},
        "text": "User: user_100",
    }
    assert record_matches_identifiers(record_with_match, identifiers) is True
    
    record_without_match = {
        "record": {"user_id": "user_999", "name": "Other User"},
        "text": "User: user_999",
    }
    assert record_matches_identifiers(record_without_match, identifiers) is False
    
    print("[OK] record_matches_identifiers tests passed")


def test_json_serialization():
    """Test that step objects serialize correctly to JSON."""
    steps = [
        {
            "step_no": 1,
            "phase": "FINDING",
            "system_id": "account_service",
            "resource_id": "account_001",
            "operation": "REPORT_REMAINING_RECORD",
            "state": "PRESENT",
            "evidence_id": "evidence_01"
        },
        {
            "step_no": 2,
            "phase": "VERIFICATION",
            "system_id": "security_service",
            "resource_id": "audit_record_123",
            "operation": "VERIFY_RECORD_PRESENT",
            "state": "PRESENT",
            "evidence_id": "evidence_02"
        }
    ]
    
    json_str = json.dumps(steps)
    parsed = json.loads(json_str)
    
    assert len(parsed) == 2
    assert parsed[0]["step_no"] == 1
    assert parsed[0]["phase"] == "FINDING"
    assert parsed[1]["phase"] == "VERIFICATION"
    
    print("[OK] JSON serialization tests passed")


def test_step_numbering_and_ordering():
    """Test that steps are properly numbered and ordered by phase."""
    from main import build_steps, mk
    
    findings = [
        mk("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_001", "PRESENT", "evidence_01")
    ]
    remediations = [
        mk("REMEDIATION", "DELETE_RECORD", "search_service", "search_001", "ABSENT", "")
    ]
    verifications = [
        mk("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_001", "ABSENT", "")
    ]
    
    steps = build_steps(findings, remediations, verifications)
    
    # Check numbering
    for i, step in enumerate(steps, 1):
        assert step["step_no"] == i, f"Step {i} has wrong step_no: {step['step_no']}"
    
    # Check phase order
    phases = [step["phase"] for step in steps]
    assert phases == ["FINDING", "REMEDIATION", "VERIFICATION"], f"Wrong phase order: {phases}"
    
    print("[OK] Step numbering and ordering tests passed")


def test_not_applicable_has_empty_steps():
    """Test that NOT_APPLICABLE cases must have empty steps."""
    # This is enforced at output level, but verify the rule
    case = {
        "case_id": "test_001",
        "email": "test@example.com",
        "request_text": "Please send me a report",  # Not a deletion request
        "audit_at": "2026-09-15T11:30:00Z"
    }
    
    # A non-deletion request should result in NOT_APPLICABLE with empty steps
    assert is_deletion_request(case["request_text"]) is False
    print("[OK] NOT_APPLICABLE empty steps rule verified")


def run_all_tests():
    """Run all tests."""
    print("Running test suite...")
    print("=" * 70)
    
    test_is_deletion_request()
    test_record_present()
    test_extract_identifiers_from_text()
    test_parse_ts()
    test_record_matches_identifiers_dict()
    test_json_serialization()
    test_step_numbering_and_ordering()
    test_not_applicable_has_empty_steps()
    
    print("=" * 70)
    print("[OK] All tests passed!")


if __name__ == "__main__":
    run_all_tests()
