"""Evidence state normalization: presence, explicit lookup results, flags and exposure.

An evidence record is a dict with keys:
    file_id, captured_at, system, store, resource_id,
    record  (the parsed content: a structured dict, {"raw": text} for logs/PDFs,
             or a lookup row {"data_category", "observed_state", "identifier"}),
    text    (searchable text form of the record).
"""

import json
import re

from .io_utils import parse_ts

# Fields that directly identify a person. Stable references (user_id,
# customer_reference, subject_reference) may remain on redacted records and tombstones.
DIRECT_IDENTIFIER_FIELDS = frozenset({
    "name", "full_name", "display_name", "email", "phone", "email_hash",
    "requester_name", "requester_email", "requester_phone",
    "customer_email", "owner_email", "owner_phone",
    "matched_value",  # PII scanner hit on a direct identifier
})

# Explicit per-resource lookup results (handover rows, and record_type/result
# lookups in JSON or logs) share one vocabulary.
LOOKUP_STATES = frozenset({
    "FOUND", "NOT_FOUND", "ACTIVE", "ANONYMIZED", "BLOCKED", "DISABLED", "IDENTIFIABLE", "REDACTED",
})

_LOG_FIELD = re.compile(r'\b([a-z_]+)="?([^\s"]*)"?')
_ABSENT_SIGNAL = re.compile(r"http_status=404\b|status_code=404\b|\brows=0\b|result=miss\b")
_PRESENT_SIGNAL = re.compile(r"http_status=200\b|status_code=200\b|\brows=[1-9]|result=hit\b")
_CLEARED_STATUSES = ("DELETED", "REMOVED", "PURGED", "ANONYMIZED", "REDACTED")


def is_raw_text(rec):
    """A log or document record: {"raw": text}."""
    return isinstance(rec, dict) and list(rec.keys()) == ["raw"]


def is_structured(rec):
    """A SQL/JSON record (not raw text and not a lookup row)."""
    return isinstance(rec, dict) and not is_raw_text(rec) and "observed_state" not in rec


def is_null(value):
    return value is None or str(value).strip().lower() in ("", "null", "none")


def iter_fields(node):
    """Yield (key, value) leaves, descending into nested objects and JSON-string payloads."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                yield from iter_fields(value)
            elif isinstance(value, str) and value.strip().startswith("{"):
                try:
                    yield from iter_fields(json.loads(value))
                except ValueError:
                    yield key, value
            else:
                yield key, value
    elif isinstance(node, list):
        for item in node:
            yield from iter_fields(item)


def record_fields(record):
    """(key, value) pairs of a record: structured leaves, or key=value tokens of log text."""
    rec = record["record"]
    if isinstance(rec, dict) and not is_raw_text(rec):
        return list(iter_fields(rec))
    return _LOG_FIELD.findall(record.get("text") or "")


def lookup_report(record):
    """{"category", "state", "identifier"} for an explicit lookup result, else None.

    In a log group the last lookup line wins, since lines are in time order."""
    rec = record["record"]
    if not isinstance(rec, dict):
        return None
    if "observed_state" in rec:
        return {"category": rec.get("data_category"), "state": rec["observed_state"],
                "identifier": rec.get("identifier", "")}
    if not is_raw_text(rec):
        state = str(rec.get("result") or "").upper()
        if rec.get("record_type") and state in LOOKUP_STATES:
            return {"category": rec["record_type"], "state": state,
                    "identifier": str(rec.get("subject_reference") or "")}
        return None
    found = None
    for line in (record.get("text") or "").replace('"', "").splitlines():
        m_type = re.search(r"\brecord_type=([A-Z_]+)", line)
        m_result = re.search(r"\bresult=([A-Za-z_]+)", line)
        if m_type and m_result and m_result.group(1).upper() in LOOKUP_STATES:
            m_ident = re.search(r"\bsubject_reference=(\S+)", line)
            found = {"category": m_type.group(1), "state": m_result.group(1).upper(),
                     "identifier": m_ident.group(1) if m_ident else ""}
    return found


def lookup_state(record):
    report = lookup_report(record)
    return report["state"] if report else None


def text_presence(text):
    """Presence from the last decisive event in a log group, or None if none is decisive.

    Cache lookups are skipped (a cache miss says nothing about the record), and other
    statuses such as 5xx are ambiguous, so they never decide presence."""
    decided = None
    for line in (text or "").lower().replace('"', "").splitlines():
        if "cache_lookup" in line or re.search(r"\bcache=", line):
            continue
        if _ABSENT_SIGNAL.search(line):
            decided = False
        elif _PRESENT_SIGNAL.search(line):
            decided = True
    return decided


def record_present(record):
    """Whether the evidence shows the record still exists. Defaults to present: missing
    or ambiguous evidence is never treated as proof of deletion."""
    state = lookup_state(record)
    if state is not None:
        return state != "NOT_FOUND"
    decided = text_presence(record["text"])
    if decided is not None:
        return decided
    rec = record["record"]
    if isinstance(rec, dict):
        status = str(rec.get("account_status", rec.get("status", "")) or "").upper()
        if status in _CLEARED_STATUSES:
            return False
        if status == "ACTIVE":
            return True
        if str(rec.get("status_code", "")) == "404":
            return False
        if "rows" in rec and str(rec.get("rows")) == "0":
            return False
        if "profile" in rec and rec.get("profile") is None:
            return False
    return True


def has_direct_identifiers(record):
    """True when a direct identifier (name, email, phone, hash, contact) is visible."""
    report = lookup_report(record)
    if report is not None:
        if report["state"] in ("ANONYMIZED", "REDACTED", "NOT_FOUND"):
            return False
        identifier = report["identifier"]
        return "@" in identifier or identifier.startswith("+")
    return any(k in DIRECT_IDENTIFIER_FIELDS and not is_null(v) for k, v in record_fields(record))


def identifier_exposure(record):
    """True if a direct identifier is visible, False if identifier fields are shown cleared
    (or the record reports itself redacted/anonymized), None if none are shown."""
    if lookup_state(record) is not None:
        return has_direct_identifiers(record)
    values = [v for k, v in record_fields(record) if k in DIRECT_IDENTIFIER_FIELDS]
    if not values:
        return None
    return any(not is_null(v) for v in values)


def field_flag(record, key):
    """Boolean value of a flag such as enabled/restore_allowed, or None if not reported."""
    for k, v in record_fields(record):
        if k == key and not is_null(v):
            return str(v).strip().lower() in ("true", "1", "yes")
    return None


def flow_enabled(record):
    """True/False when the evidence reports the connector state, None when it is silent."""
    state = lookup_state(record)
    if state in ("ACTIVE", "DISABLED"):
        return state == "ACTIVE"
    for key in ("enabled", "active"):
        flag = field_flag(record, key)
        if flag is not None:
            return flag
    return None


def restore_allowed(record):
    """True/False when the evidence reports restore access, None when it is silent."""
    state = lookup_state(record)
    if state in ("ACTIVE", "BLOCKED"):
        return state == "ACTIVE"
    return field_flag(record, "restore_allowed")


def observation_time(record):
    """The record's own observation time (recorded_at/observed_at), which can be later
    than its file's capture time; None when the record does not state one."""
    rec = record["record"]
    if isinstance(rec, dict):
        for key in ("recorded_at", "observed_at"):
            if rec.get(key):
                return parse_ts(str(rec[key]))
    return None
