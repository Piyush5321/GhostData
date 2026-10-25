"""Subject resolution and whole-token identifier matching."""

import functools
import re

from .state import is_structured, iter_fields

IDENTITY_FIELDS = ("email", "phone", "email_hash")

# Characters that may surround an identifier without being part of it. Requiring
# these boundaries stops user_11 matching user_110 and ann@x.com matching joann@x.com.
_LEFT_BOUNDARY = r"(?<![a-z0-9._%+-])"
_RIGHT_BOUNDARY = r"(?![a-z0-9])"


def build_identity_index(identities, audit_log):
    """Map current emails, historical emails and user_ids to the stable user_id.

    user_audit_log.csv records changes as (field_name, old_value, new_value), so a
    request may arrive from an address that only appears there. The current
    snapshot wins if an address was later reused.
    """
    index = {}
    for row in identities:
        user_id = row.get("user_id", "").strip()
        if not user_id:
            continue
        index[user_id.lower()] = user_id
        email = row.get("email", "").strip().lower()
        if email:
            index[email] = user_id
    for row in audit_log:
        user_id = row.get("user_id", "").strip()
        if not user_id or row.get("field_name", "").strip() != "email":
            continue
        for value in (row.get("old_value"), row.get("new_value")):
            value = (value or "").strip().lower()
            if value and value not in index:
                index[value] = user_id
    return index


def resolve_subject(email, identity_index, identities, audit_log):
    """Return (user_id, identifiers): the stable id and every current or historical
    email, phone and email hash that older systems and evidence may still hold."""
    email = email.strip().lower()
    identifiers = {email}
    user_id = identity_index.get(email)
    if not user_id:
        return None, identifiers
    identifiers.add(user_id.lower())
    for row in identities:
        if row.get("user_id", "").strip() == user_id:
            identifiers.update(_clean(row.get(field)) for field in IDENTITY_FIELDS)
    for row in audit_log:
        if row.get("user_id", "").strip() == user_id and row.get("field_name", "").strip() in IDENTITY_FIELDS:
            identifiers.update((_clean(row.get("old_value")), _clean(row.get("new_value"))))
    identifiers.discard("")
    return user_id, identifiers


def _clean(value):
    return (value or "").strip().lower()


def contains_identifier(text, identifier):
    """Case-insensitive whole-token search for one identifier."""
    if not identifier or not text:
        return False
    pattern = _LEFT_BOUNDARY + re.escape(identifier.lower()) + _RIGHT_BOUNDARY
    return re.search(pattern, text.lower()) is not None


@functools.lru_cache(maxsize=4096)
def identifier_pattern(identifiers):
    """One compiled whole-token regex for a frozenset of lower-case identifiers."""
    if not identifiers:
        return None
    alternation = "|".join(re.escape(i) for i in sorted(identifiers, key=len, reverse=True))
    return re.compile(_LEFT_BOUNDARY + "(?:" + alternation + ")" + _RIGHT_BOUNDARY)


def record_match_text(record):
    """Lower-cased searchable text of an evidence record, cached on the record.

    Structured records contribute one field value per line, so a token never spans
    two fields; log and handover records contribute their text."""
    cached = record.get("_match_text")
    if cached is None:
        rec = record["record"]
        if is_structured(rec):
            cached = "\n".join(str(v) for _, v in iter_fields(rec) if v is not None)
        else:
            raw = rec.get("raw") if isinstance(rec, dict) else None
            cached = (record.get("text") or "") + "\n" + (raw if isinstance(raw, str) else "")
        cached = cached.lower()
        record["_match_text"] = cached
    return cached


def record_matches_identifiers(record, identifiers):
    """True when the record carries one of the subject's identifiers as a whole token."""
    pattern = identifier_pattern(frozenset(i.lower() for i in identifiers if i))
    return pattern is not None and pattern.search(record_match_text(record)) is not None
