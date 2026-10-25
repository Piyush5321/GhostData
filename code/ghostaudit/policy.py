"""Data category of an evidence record and the action deletion_rules.csv requires for it."""

from .state import lookup_report

ATTRIBUTABLE_CONDITION = "Record is still attributable to a user"

# Canonical system -> data category (support records are split by content below).
SYSTEM_CATEGORIES = {
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "export_service": "USER_EXPORT",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "account_service": "USER_PROFILE",
    "security_service": "SECURITY_AUDIT_LOG",
    "notification_service": "EMAIL_DELIVERY_LOG",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "marketing_platform": "MARKETING_PROFILE",
}

# Fallbacks for records whose system is unknown: keyword in resource id or collection name.
KEYWORD_CATEGORIES = (
    ("tombstone", "ACCOUNT_TOMBSTONE"),
    ("data_flow", "DATA_FLOW"),
    ("connector", "DATA_FLOW"),
    ("backup", "BACKUP_ARCHIVE"),
    ("export", "USER_EXPORT"),
    ("search", "SEARCH_INDEX_ENTRY"),
    ("analytics", "ANALYTICS_PROFILE"),
    ("support", "SUPPORT_MESSAGE"),
    ("account", "USER_PROFILE"),
    ("profile", "USER_PROFILE"),
    ("audit", "SECURITY_AUDIT_LOG"),
    ("security", "SECURITY_AUDIT_LOG"),
    ("legal", "LEGAL_HOLD_RECORD"),
    ("hold", "LEGAL_HOLD_RECORD"),
)


def category_for_record(record):
    """Data category: an explicit lookup category first, then the owning system,
    then keywords in the resource id or collection name. Defaults to USER_PROFILE."""
    report = lookup_report(record)
    if report and report["category"]:
        return report["category"]
    system = record["system"]
    if system == "support_platform":
        return _support_category(record)
    if system in SYSTEM_CATEGORIES:
        return SYSTEM_CATEGORIES[system]
    rec = record["record"]
    collection = str(rec.get("collection_name") or "").lower() if isinstance(rec, dict) else ""
    for name in (record["resource_id"].lower(), collection):
        for keyword, category in KEYWORD_CATEGORIES:
            if keyword in name:
                return _support_category(record) if category == "SUPPORT_MESSAGE" else category
    return "USER_PROFILE"


def _support_category(record):
    text = record["text"].lower()
    if "attachment" in text or ".pdf" in text or "file=" in text:
        return "SUPPORT_ATTACHMENT"
    return "SUPPORT_MESSAGE"


def load_rules(rules_rows):
    """deletion_rules.csv rows indexed by data category."""
    return {row["data_category"].strip(): row for row in rules_rows}


def required_action(category, record, rules):
    """DELETE, REDACT, ANONYMIZE or RETAIN for a record of this category.

    Every rule's action applies as written, except that an anonymization rule
    conditioned on attribution does not apply to a record that is no longer
    attributable. Categories without a rule default to DELETE."""
    rule = rules.get(category)
    if rule is None:
        return "DELETE"
    if rule["condition"].strip() == ATTRIBUTABLE_CONDITION and not record_is_attributable(record):
        return "RETAIN"
    return rule["required_action"].strip()


def record_is_attributable(record):
    rec = record["record"]
    if isinstance(rec, dict):
        for field in ("user_id", "email", "phone", "email_hash", "customer_reference", "subject_reference"):
            if rec.get(field) not in (None, "", "null"):
                return True
    return bool(record["text"].strip())
