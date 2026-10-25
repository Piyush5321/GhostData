#!/usr/bin/env python3
import csv, functools, json, os, re, shutil, sqlite3, subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DATASET = os.path.join(PROJECT_ROOT, "dataset")
EVIDENCE_DIR = os.path.join(DATASET, "evidence")
OUTPUT = os.path.join(PROJECT_ROOT, "output.csv")


def load_csv(path):
    if not os.path.isabs(path):
        path = os.path.join(DATASET, path)

    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


IDENTITY_FIELDS = ("email", "phone", "email_hash")


def build_identity_index(identities, audit_log):
    """Map current emails, historical emails and user_ids to the stable user_id.

    user_audit_log.csv records changes as (field_name, old_value, new_value), so a
    request may arrive from an address that only appears there.
    """
    id_to_user = {}
    user_ids = set()
    for row in identities:
        user_id = row.get("user_id", "").strip()
        if not user_id:
            continue
        user_ids.add(user_id.lower())
        id_to_user[user_id.lower()] = user_id
        email = row.get("email", "").strip().lower()
        if email:
            id_to_user[email] = user_id
    for row in audit_log:
        user_id = row.get("user_id", "").strip()
        if not user_id or row.get("field_name", "").strip() != "email":
            continue
        for value in (row.get("old_value"), row.get("new_value")):
            value = (value or "").strip().lower()
            # The current snapshot wins if an address was later reused
            if value and value not in id_to_user:
                id_to_user[value] = user_id
    return id_to_user, user_ids


def parse_ts(s):
    if not s:
        return None
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        return None


def parse_json_file(path, file_id, captured_at):
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return records

    def walk(node, system_hint=None):
        if isinstance(node, dict):
            if "resource_id" in node:
                inferred_system = node.get("system_id") or node.get("service") or system_hint
                if not inferred_system:
                    ds = str(node.get("data_store", "") or "").lower()
                    sr = str(node.get("subject_reference", "") or "")
                    if "backup" in ds or "snapshot" in ds:
                        inferred_system = "backup_service"
                    elif "analytics" in ds or "profile" in ds:
                        inferred_system = "analytics_platform"
                    elif "search" in ds:
                        inferred_system = "search_service"
                    elif "export" in ds:
                        inferred_system = "export_service"
                    elif "account" in ds or "profile" in ds:
                        inferred_system = "account_service"
                    elif "log" in ds:
                        inferred_system = "log_service"
                    else:
                        inferred_system = system_hint or "unknown_service"
                record = {
                    "file_id": file_id,
                    "captured_at": captured_at,
                    "system": inferred_system,
                    "resource_id": node.get("resource_id", "") or node.get("id", ""),
                    "record": node,
                    "text": json.dumps(node, ensure_ascii=False),
                }
                if "raw" in node:
                    record["raw"] = node["raw"]
                records.append(record)
            for v in node.values():
                walk(v, system_hint)
        elif isinstance(node, list):
            for item in node:
                walk(item, system_hint)

    # JSON bundles are extracts, not full store dumps, so omission from them is not
    # treated as proof of absence (only SQL snapshots provide coverage).
    walk(data)
    return records


PRIMARY_STORE = "primary"


def store_of(name):
    """Replica tables/services are separate copies; everything else reads the primary store."""
    name = (name or "").lower()
    return name if "replica" in name else PRIMARY_STORE


def coverage_record(file_id, captured_at, stores, resource_ids):
    """A snapshot that lists every record of the given (system, store) pairs."""
    return {
        "kind": "coverage",
        "file_id": file_id,
        "captured_at": captured_at,
        "stores": set(stores),
        "resource_ids": {r for r in resource_ids if r},
    }


def parse_sql_file(path, file_id, captured_at):
    records = []
    covered = set()
    present_ids = set()
    scope = None
    try:
        conn = sqlite3.connect(":memory:")
        with open(path, encoding="utf-8", errors="replace") as f:
            conn.executescript(f.read())
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        for table in tables:
            try:
                cur.execute(f"SELECT * FROM {table}")
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
            except sqlite3.Error:
                continue
            if table == "snapshot_scope":
                scope = {r[0] for r in rows if r and r[0]}
                covered.update((s, PRIMARY_STORE) for s in scope)
                continue
            store = store_of(table)
            if "origin_system" not in columns:
                # Typed tables (customer_profiles, ...) cover their store even when empty
                covered.add((system_from_table(table), store))
            for row in rows:
                rec = dict(zip(columns, row))
                rec_system = rec.get("origin_system") or rec.get("system") or ""
                resource_id = rec.get("resource_id") or rec.get("id") or ""
                present_ids.add(resource_id)
                covered.add((canonical_system(resource_id, rec_system or system_from_table(table)), store))
                records.append({
                    "file_id": file_id,
                    "captured_at": captured_at,
                    "system": rec_system or system_from_table(table),
                    "store": store,
                    "table": table,
                    "resource_id": resource_id,
                    "record": rec,
                    "text": json.dumps(rec, ensure_ascii=False),
                })
    except (sqlite3.Error, OSError):
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # A declared snapshot_scope limits completeness to the listed systems; empty typed
    # tables outside that scope were simply not captured, not emptied.
    covered = {(s, store) for s, store in covered
               if s != "unknown_service" and (scope is None or s in scope)}
    if covered:
        records.append(coverage_record(file_id, captured_at, covered, present_ids))
    return records


def system_from_table(table_name):
    t = table_name.lower()
    if "backup" in t or "snapshot" in t:
        return "backup_service"
    # Check analytics before profile: customer_analytics_profiles is analytics,
    # customer_profiles_* is the account store.
    if "analytics" in t:
        return "analytics_platform"
    if "search" in t:
        return "search_service"
    if "export" in t:
        return "export_service"
    if "account" in t or "profile" in t:
        return "account_service"
    if "log" in t:
        return "log_service"
    if "audit" in t:
        return "security_service"
    return "unknown_service"


def normalize_log_line(line):
    """Rewrite the supported layouts as 'ts service=... trace_id=... key="value" ...'.

    Layouts: key=value lines; '[LEVEL] [service] [trace]' lines (handled by the
    caller's regexes); JSON-lines 'ts {...}'; pipe 'ts | LEVEL | service | trace | event | ...';
    RFC 5424 syslog 'ts <pri>1 host service trace - - ...'; and
    'ts requestId=trace ... function=service message="key=\\"value\\" ..."'.
    """
    line = line.replace('\\"', '"')
    m = re.match(r"^(\S+)\s+(\{.*\})\s*$", line)
    if m:
        try:
            data = json.loads(m.group(2))
        except ValueError:
            data = None
        if isinstance(data, dict):
            return m.group(1) + " " + " ".join(f'{k}="{v}"' for k, v in data.items())
    m = re.match(r"^(\S+) \| (\w+) \| ([a-z_]+) \| (\S+) \| (\S+) \| (.*)$", line)
    if m:
        return f"{m.group(1)} level={m.group(2)} service={m.group(3)} trace_id={m.group(4)} event={m.group(5)} {m.group(6)}"
    m = re.match(r"^(\S+) <\d+>\d+ \S+ ([a-z_]+) (\S+) - - (.*)$", line)
    if m:
        return f"{m.group(1)} service={m.group(2)} trace_id={m.group(3)} {m.group(4)}"
    m = re.match(r'^(\S+) requestId=(\S+) (.*?)function=([a-z_]+) message="(.*)"\s*$', line)
    if m:
        return f"{m.group(1)} trace_id={m.group(2)} service={m.group(4)} {m.group(3)}{m.group(5)}"
    return line


def parse_log_file(path, file_id, captured_at):
    """Group log lines into one record per (trace, resource).

    Lines of a trace that name no resource (e.g. the lookup by email) are shared by
    every resource of that trace; lines naming a different resource are not, so one
    resource's result is never read as another's."""
    traces = {}
    order = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    for raw_line in lines:
        line = normalize_log_line(raw_line.rstrip("\n"))
        if not line.strip():
            continue
        m_sys = re.search(r'\bservice="?([a-z_]+)', line) or re.search(r"\] \[([a-z_]+)\] \[", line)
        m_res = re.search(r'resource_id="?([A-Za-z0-9_]+)', line)
        m_trace = re.search(r'trace_id="?([\w.-]+)', line) or re.search(r"\[(tr-[^\]]+)\]", line)
        system = m_sys.group(1) if m_sys else "unknown_service"
        resource_id = m_res.group(1) if m_res else ""
        if m_trace:
            key = m_trace.group(1)
        elif resource_id:
            key = resource_id
        else:
            key = "__line__" + str(len(order))
        if key not in traces:
            traces[key] = []
            order.append(key)
        traces[key].append((line, system, resource_id))

    records = []
    for key in order:
        entries = traces[key]
        resources = list(dict.fromkeys(r for _, _, r in entries if r)) or [""]
        for resource_id in resources:
            own = [(l, s) for l, s, r in entries if r in ("", resource_id)]
            systems = [s for l, s, r in entries if r == resource_id and s != "unknown_service"] or \
                      [s for _, s in own if s != "unknown_service"] or ["unknown_service"]
            text = "\n".join(l for l, _ in own)
            records.append({
                "file_id": file_id,
                "captured_at": captured_at,
                "system": systems[-1],
                "resource_id": resource_id,
                "record": {"raw": text},
                "text": text,
            })
    return records


HANDOVER_LINE = re.compile(
    r"^-\s*(\S+)\s*\|\s*([a-z_]+)\s*\|\s*([A-Za-z0-9_]+)\s*\|\s*([A-Z_]+)\s*\|\s*([A-Z_]+)\s*\|\s*(.+?)\s*$"
)


def parse_text_file(path, file_id, captured_at):
    """Parse '- ts | system | resource | CATEGORY | STATE | identifier' handover rows;
    fall back to log parsing for other text files."""
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return records
    for line in lines:
        m = HANDOVER_LINE.match(line.strip())
        if not m:
            continue
        _, system, resource_id, category, state, identifier = m.groups()
        records.append({
            "file_id": file_id,
            "captured_at": captured_at,
            "system": system,
            "resource_id": resource_id,
            "record": {"data_category": category, "observed_state": state, "identifier": identifier},
            "text": line.strip(),
        })
    return records or parse_log_file(path, file_id, captured_at)


RESOURCE_ID_PATTERN = re.compile(
    r"\b(?:audit_record|tombstone|data_flow|account|analytics|search|support|export|backup|marketing)_\d+\b"
)


def parse_pdf_file(path, file_id, captured_at):
    """Extract PDF text with poppler's pdftotext when it is installed (optional).

    A document becomes evidence only when it names exactly one resource, so a
    multi-subject register cannot link unrelated people to a resource.
    """
    exe = shutil.which("pdftotext")
    if not exe:
        return []
    try:
        result = subprocess.run([exe, "-layout", path, "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    text = result.stdout.decode("utf-8", errors="replace")
    resource_ids = set(RESOURCE_ID_PATTERN.findall(text))
    if len(resource_ids) != 1:
        return []
    return [{
        "file_id": file_id,
        "captured_at": captured_at,
        "system": "unknown_service",
        "resource_id": resource_ids.pop(),
        "record": {"raw": text},
        "text": text,
    }]


def parse_evidence_file(file_id, rel_path, captured_at):
    ext = os.path.splitext(rel_path)[1].lower()
    # Remove leading "evidence/" if present, since EVIDENCE_DIR already points to the evidence directory
    if rel_path.startswith("evidence/"):
        rel_path = rel_path[9:]  # len("evidence/") == 9
    elif rel_path.startswith("evidence\\"):
        rel_path = rel_path[9:]
    path = os.path.join(EVIDENCE_DIR, rel_path)
    if ext == ".sql":
        return parse_sql_file(path, file_id, captured_at)
    if ext == ".json":
        return parse_json_file(path, file_id, captured_at)
    if ext == ".txt":
        return parse_text_file(path, file_id, captured_at)
    if ext == ".pdf":
        return parse_pdf_file(path, file_id, captured_at)
    if ext == ".log":
        return parse_log_file(path, file_id, captured_at)
    return []


# Resource-id prefixes name the owning system; replica/store names such as
# support_replica or customer_profiles_failover_replica map back to it.
RESOURCE_SYSTEMS = (
    ("audit_record_", "security_service"),
    ("tombstone_", "identity_service"),
    ("data_flow_", "integration_service"),
    ("account_", "account_service"),
    ("analytics_", "analytics_platform"),
    ("search_", "search_service"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("backup_", "backup_service"),
    ("marketing_", "marketing_platform"),
)


def canonical_system(resource_id, fallback):
    rid = (resource_id or "").lower()
    for prefix, system in RESOURCE_SYSTEMS:
        if rid.startswith(prefix):
            return system
    return fallback


def load_all_evidence(evidence_manifest):
    all_records = []
    for row in evidence_manifest:
        file_id = row["file_id"].strip()
        rel_path = row["file_path"].strip()
        captured_at = parse_ts(row.get("captured_at", ""))
        for rec in parse_evidence_file(file_id, rel_path, captured_at):
            if rec.get("kind") != "coverage":
                # Logs name the reading service (e.g. support_replica); keep it as the store
                rec.setdefault("store", store_of(rec["system"]))
                rec["system"] = canonical_system(rec["resource_id"], rec["system"])
            all_records.append(rec)
    return all_records


def load_rules(rules_rows):
    return rules_rows


def contains_identifier(text, ident):
    """Case-insensitive match of ident in text as a whole token.

    Prevents e.g. user_11 matching user_110, or ann@x.com matching joann@x.com.
    """
    if not ident or not text:
        return False
    pattern = r"(?<![a-z0-9._%+-])" + re.escape(ident.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None


def extract_identifiers_from_text(text, identifiers, raw=None):
    """Return set of subject identifiers found in text (case-insensitive)."""
    found = set()
    for ident in identifiers:
        if contains_identifier(text, ident) or (raw is not None and contains_identifier(raw, ident)):
            found.add(ident)
    return found


def category_for_record(record, rules_by_category):
    system = record["system"]
    resource_id = record["resource_id"].lower()
    rec = record["record"]
    report = lookup_report(record)
    if report and report["category"]:
        return report["category"]
    if system == "identity_service" or "tombstone" in resource_id:
        return "ACCOUNT_TOMBSTONE"
    if system == "integration_service" or "data_flow" in resource_id or "connector" in resource_id:
        return "DATA_FLOW"
    if system == "backup_service" or "backup" in resource_id:
        return "BACKUP_ARCHIVE"
    if system == "export_service" or "export" in resource_id:
        return "USER_EXPORT"
    if system == "search_service" or "search" in resource_id:
        return "SEARCH_INDEX_ENTRY"
    if system == "analytics_platform" or "analytics" in resource_id:
        return "ANALYTICS_PROFILE"
    if system == "support_platform" or "support" in resource_id:
        txt = record["text"].lower()
        if "attachment" in txt or ".pdf" in txt or "file=" in txt:
            return "SUPPORT_ATTACHMENT"
        return "SUPPORT_MESSAGE"
    if system == "account_service" or system == "identity_service" or "account" in resource_id:
        return "USER_PROFILE"
    if system == "security_service" or "audit" in resource_id:
        return "SECURITY_AUDIT_LOG"
    if "legal" in resource_id or "hold" in resource_id:
        return "LEGAL_HOLD_RECORD"
    if system in ("notification_service", "email_delivery_service"):
        return "EMAIL_DELIVERY_LOG"
    if system == "marketing_platform":
        return "MARKETING_PROFILE"
    coll = ""
    if isinstance(rec, dict):
        coll = str(rec.get("collection_name", "") or "").lower()
    if "audit" in coll or "security" in coll:
        return "SECURITY_AUDIT_LOG"
    if "backup" in coll:
        return "BACKUP_ARCHIVE"
    if "export" in coll:
        return "USER_EXPORT"
    if "search" in coll:
        return "SEARCH_INDEX_ENTRY"
    if "analytics" in coll:
        return "ANALYTICS_PROFILE"
    if "support" in coll:
        return "SUPPORT_MESSAGE"
    if "account" in coll or "profile" in coll:
        return "USER_PROFILE"
    return "USER_PROFILE"


def required_action_for(category, condition_text, record, rules_rows):
    for r in rules_rows:
        if r.get("data_category", "").strip() != category:
            continue
        cond = (r.get("condition") or "").strip()
        action = (r.get("required_action") or "").strip()
        if cond == "Always":
            return action
        if cond == "Account is deleted and only a stable internal user_id is needed to preserve references to retained records":
            return action
        if cond == "Restore access can be blocked":
            return "RETAIN"
        if cond == "Record is within the supplied retention period":
            return "RETAIN"
        if cond == "A supplied legal hold is active":
            return "RETAIN"
        if cond == "Record is still attributable to a user":
            if record_is_attributable(record):
                return "ANONYMIZE"
            return "RETAIN"
        if cond == "Conversation contains shared customer, agent, or order context that must be retained":
            return "REDACT"
        if cond == "Attachment belongs only to the deletion subject":
            return "DELETE"
        if cond == "Log contains a user email address":
            return "REDACT"
    return "DELETE"


# Fields that directly identify a person. Stable references (user_id,
# customer_reference, subject_reference) may remain on redacted records and tombstones.
DIRECT_IDENTIFIER_FIELDS = {
    "name", "full_name", "display_name", "email", "phone", "email_hash",
    "requester_name", "requester_email", "requester_phone",
    "customer_email", "owner_email", "owner_phone",
    "matched_value",  # PII scanner hit on a direct identifier
}
LOG_FIELD = re.compile(r'\b([a-z_]+)="?([^\s"]*)"?')


def is_null(value):
    return value is None or str(value).strip().lower() in ("", "null", "none")


def iter_fields(node):
    """Yield (key, value) leaves of a record, descending into nested and JSON-string payloads."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                yield from iter_fields(v)
            elif isinstance(v, str) and v.strip().startswith("{"):
                try:
                    yield from iter_fields(json.loads(v))
                except ValueError:
                    yield k, v
            else:
                yield k, v
    elif isinstance(node, list):
        for item in node:
            yield from iter_fields(item)


def record_fields(record):
    rec = record["record"]
    if isinstance(rec, dict) and list(rec.keys()) != ["raw"]:
        return list(iter_fields(rec))
    return LOG_FIELD.findall(record.get("text") or "")


# Explicit lookup results reported per resource (handover rows, and record_type/result
# lookups in JSON or logs) share one vocabulary.
LOOKUP_STATES = {"FOUND", "NOT_FOUND", "ACTIVE", "ANONYMIZED", "BLOCKED", "DISABLED", "IDENTIFIABLE", "REDACTED"}


def lookup_report(record):
    """{category, state, identifier} for an explicit per-resource lookup result, else None.

    In a log group the last lookup line wins, since lines are in time order."""
    rec = record["record"]
    if not isinstance(rec, dict):
        return None
    if "observed_state" in rec:
        return {"category": rec.get("data_category"), "state": rec["observed_state"],
                "identifier": rec.get("identifier", "")}
    if list(rec.keys()) != ["raw"]:
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


def handover_state(record):
    report = lookup_report(record)
    return report["state"] if report else None


def has_direct_identifiers(record):
    report = lookup_report(record)
    if report is not None:
        if report["state"] in ("ANONYMIZED", "REDACTED", "NOT_FOUND"):
            return False
        ident = report["identifier"]
        return "@" in ident or ident.startswith("+")
    return any(k in DIRECT_IDENTIFIER_FIELDS and not is_null(v) for k, v in record_fields(record))


def identifier_exposure(record):
    """True if a direct identifier is visible, False if the record shows its identifier
    fields cleared (or reports itself redacted/anonymized), None if it shows neither."""
    state = handover_state(record)
    if state is not None:
        return has_direct_identifiers(record)
    values = [v for k, v in record_fields(record) if k in DIRECT_IDENTIFIER_FIELDS]
    if not values:
        return None
    return any(not is_null(v) for v in values)


def tombstone_has_direct_identifiers(record):
    return has_direct_identifiers(record)


def field_flag(record, key):
    """Boolean value of a flag such as enabled/restore_allowed, or None if not reported."""
    for k, v in record_fields(record):
        if k == key and not is_null(v):
            return str(v).strip().lower() in ("true", "1", "yes")
    return None


def record_is_attributable(record):
    text = record["text"]
    rec = record["record"]
    if isinstance(rec, dict):
        for f in ("user_id", "email", "phone", "email_hash", "customer_reference", "subject_reference"):
            v = rec.get(f)
            if v not in (None, "", "null"):
                return True
    return bool(text.strip())


ABSENT_SIGNAL = re.compile(r"http_status=404\b|status_code=404\b|\brows=0\b|result=miss\b")
PRESENT_SIGNAL = re.compile(r"http_status=200\b|status_code=200\b|\brows=[1-9]|result=hit\b")


def text_presence(text):
    """Presence from the last decisive event in a log group, or None if none is decisive.

    Cache lookups are skipped (a cache miss says nothing about the record) and other
    statuses such as 5xx are ambiguous, so they never decide presence."""
    decided = None
    for line in (text or "").lower().replace('"', "").splitlines():
        if "cache_lookup" in line or re.search(r"\bcache=", line):
            continue
        if ABSENT_SIGNAL.search(line):
            decided = False
        elif PRESENT_SIGNAL.search(line):
            decided = True
    return decided


def record_present(record):
    rec = record["record"]
    state = handover_state(record)
    if state is not None:
        return state != "NOT_FOUND"
    decided = text_presence(record["text"])
    if decided is not None:
        return decided
    if isinstance(rec, dict):
        status = str(rec.get("account_status", rec.get("status", "")) or "").upper()
        if status in ("DELETED", "REMOVED", "PURGED", "ANONYMIZED", "REDACTED"):
            return False
        if status == "ACTIVE":
            return True
        if "status_code" in rec:
            if rec.get("status_code") == 404 or rec.get("status_code") == "404":
                return False
        if "rows" in rec:
            if rec.get("rows") == 0 or rec.get("rows") == "0":
                return False
        if "profile" in rec and rec.get("profile") is None:
            return False
        if rec.get("result") == "hit":
            return True
    return True


def flow_enabled(record):
    """True/False when the evidence reports the connector state, None when it does not."""
    state = handover_state(record)
    if state in ("ACTIVE", "DISABLED"):
        return state == "ACTIVE"
    for key in ("enabled", "active"):
        flag = field_flag(record, key)
        if flag is not None:
            return flag
    return None


def restore_allowed(record):
    """True/False when the evidence reports restore access, None when it does not."""
    state = handover_state(record)
    if state in ("ACTIVE", "BLOCKED"):
        return state == "ACTIVE"
    return field_flag(record, "restore_allowed")


LINK_DETAILS = re.compile(r"linked (\S+) \(.*?\) to (\S+) and (user_\w+)|confirmed (\S+) \(.*?\) belongs only to (\S+) and (user_\w+)")


def linked_resources(identifiers, audit_at, system_activity):
    """Resource ids that the data catalog explicitly linked to the subject by audit_at."""
    linked = set()
    for row in system_activity:
        if row.get("event_type") != "LINK_RESOURCE_IDENTITY":
            continue
        t = parse_ts(row.get("event_time", ""))
        if t is not None and audit_at is not None and t > audit_at:
            continue
        m = LINK_DETAILS.search(row.get("details", ""))
        if not m:
            continue
        rid, email, user = [g for g in m.groups() if g]
        if any(contains_identifier(email, i) or contains_identifier(user, i) for i in identifiers):
            linked.add(rid)
    return linked


def observation_time(record):
    """A record's own observation time (recorded_at/observed_at), which can be later
    than its file's capture time; None when the record does not state one."""
    rec = record["record"]
    if isinstance(rec, dict):
        for key in ("recorded_at", "observed_at"):
            if rec.get(key):
                return parse_ts(str(rec[key]))
    return None


def find_relevant_records(case_email, identifiers, audit_at, evidence_records, system_activity):
    linked = linked_resources(identifiers, audit_at, system_activity)
    relevant = []
    for rec in evidence_records:
        if rec.get("kind") == "coverage":
            continue
        cap = rec["captured_at"]
        if cap is not None and audit_at is not None and cap > audit_at:
            continue
        observed = observation_time(rec)
        if observed is not None and audit_at is not None and observed > audit_at:
            continue
        if rec["resource_id"] in linked or record_matches_identifiers(rec, identifiers):
            relevant.append(rec)
    return relevant


RETAINED_SYSTEMS = {"security_service"}


def latest_observations(relevant, coverage, audit_at):
    """Pick the observation that decides each (system, resource) at audit_at.

    Each store (primary, or a named replica) is judged by its latest admissible
    observation; a later snapshot that covers the store but no longer lists the
    resource counts as an absent observation. The resource remains if any store's
    latest observation still holds it, so a primary readback cannot hide a replica copy.
    """
    floor = parse_ts("0001-01-01T00:00:00+00:00")
    by_key = {}
    for rec in relevant:
        by_key.setdefault((rec["system"], rec["resource_id"]), {}).setdefault(
            rec.get("store", PRIMARY_STORE), []).append(rec)
    chosen = {}
    for (system, resource_id), stores in by_key.items():
        store_latest = []
        for store, recs in stores.items():
            latest = max(r["captured_at"] or floor for r in recs)
            group = [r for r in recs if (r["captured_at"] or floor) == latest]
            present = [r for r in group if record_present(r)]
            exposed = [r for r in present if has_direct_identifiers(r)]
            best = (exposed or present or group)[0]
            # Omission from a snapshot can prove a removal, but is not used to claim
            # that a record policy requires us to retain has gone missing.
            later = [] if system in RETAINED_SYSTEMS else [
                s for s in coverage
                if s["captured_at"] and s["captured_at"] > latest
                and (audit_at is None or s["captured_at"] <= audit_at)
                and (system, store) in s["stores"] and resource_id not in s["resource_ids"]
            ]
            if later:
                snap = max(later, key=lambda s: s["captured_at"])
                best = {
                    "file_id": snap["file_id"],
                    "captured_at": snap["captured_at"],
                    "system": system,
                    "store": store,
                    "resource_id": resource_id,
                    "record": {"observed_state": "NOT_FOUND"},
                    "text": "",
                }
            store_latest.append(best)
        newest_first = sorted(store_latest, key=lambda r: r["captured_at"] or floor, reverse=True)
        remaining = [r for r in newest_first if record_present(r)]
        exposed = [r for r in remaining if has_direct_identifiers(r)]
        chosen[(system, resource_id)] = (exposed or remaining or newest_first)[0]
    return chosen


def build_steps(findings, remediations, verifications):
    steps = []
    step_no = 0

    def emit(phase, op, system_id, resource_id, state, evidence_id):
        nonlocal step_no
        step_no += 1
        steps.append({
            "step_no": step_no,
            "phase": phase,
            "system_id": system_id,
            "resource_id": resource_id,
            "operation": op,
            "state": state,
            "evidence_id": evidence_id,
        })

    def sort_key(s):
        op_order = OPERATION_ORDER[s["phase"]]
        try:
            oi = op_order.index(s["operation"])
        except ValueError:
            oi = len(op_order)
        return (oi, s["system_id"], s["resource_id"])

    for phase in ("FINDING", "REMEDIATION", "VERIFICATION"):
        group = {"FINDING": findings, "REMEDIATION": remediations, "VERIFICATION": verifications}[phase]
        for s in sorted(group, key=sort_key):
            emit(phase, s["operation"], s["system_id"], s["resource_id"], s["state"], s["evidence_id"])
    return steps


def mk(phase, operation, system_id, resource_id, state, evidence_id):
    return {
        "phase": phase,
        "operation": operation,
        "system_id": system_id,
        "resource_id": resource_id,
        "state": state,
        "evidence_id": evidence_id,
    }


def resolve_subject(email, id_to_user, audit_log, identities=()):
    """Return the subject's user_id and every current or historical identifier
    (email, phone, email_hash) that older systems and evidence may still hold."""
    email_lower = email.strip().lower()
    identifiers = {email_lower}
    user_id = id_to_user.get(email_lower)
    if not user_id:
        return None, identifiers
    identifiers.add(user_id.lower())
    for row in identities:
        if row.get("user_id", "").strip() == user_id:
            for field in IDENTITY_FIELDS:
                value = (row.get(field) or "").strip().lower()
                if value:
                    identifiers.add(value)
    for row in audit_log:
        if row.get("user_id", "").strip() == user_id and row.get("field_name", "").strip() in IDENTITY_FIELDS:
            for value in (row.get("old_value"), row.get("new_value")):
                value = (value or "").strip().lower()
                if value:
                    identifiers.add(value)
    return user_id, identifiers


DELETION_NEGATION_PATTERNS = [
    # Explicit refusal of a deletion verb, e.g. "please don't delete or change anything"
    r"\b(?:do not|don't|dont|should not|shouldn't|never)\s+(?:delete|erase|close|remove|deactivate)",
    # "I am not asking for deletion", "not a request to erase it"
    r"\bnot (?:a request|asking|requesting) (?:for|to) (?:the )?(?:delet|eras|remov|clos)",
    r"\bwithout (?:deleting|removing|erasing|closing)\b",
]

# Stems so that delete / deleted / deleting / deletion all match.
DELETION_PATTERNS = [
    r"\bdelet", r"\beras(?:e|ed|ing|ure)\b", r"\bremov", r"\bpurg", r"\bdestroy",
    r"right to be forgotten",
    r"\b(?:account|profile|data) (?:is|was|are|were) (?:gone|deleted|removed)\b",
    # Spanish / Portuguese / French / German / Italian
    r"\belimina", r"\bborra", r"\bexclus[ãa]o\b", r"\bexclu[ií]r", r"\bapaga",
    r"\bsuppression\b", r"\bsupprim", r"\beffac", r"\bl[öo]sch", r"\bcancellazion",
    # Japanese / Korean / Chinese
    r"削除", r"삭제", r"删除", r"刪除",
]

ACCOUNT_CLOSURE_PATTERN = r"\bclos(?:e|ed|ing|ure)\b"
ACCOUNT_CONTEXT = ["account", "profile", "data", "information", "personal"]


def is_deletion_request(request_text):
    if not request_text:
        return False
    text = request_text.strip().lower().replace("’", "'")

    if any(re.search(p, text) for p in DELETION_NEGATION_PATTERNS):
        return False

    if any(re.search(p, text) for p in DELETION_PATTERNS):
        return True

    # "close" only counts when it is about the account or personal data
    if re.search(ACCOUNT_CLOSURE_PATTERN, text):
        return any(ctx in text for ctx in ACCOUNT_CONTEXT)

    # Access, correction, export, opt-out and other requests do not authorize deletion
    return False


def record_matches_identifiers(record, identifiers):
    """Check whether a record carries one of the subject's identifiers.

    Structured records (SQL/JSON, including JSON payload columns) are matched
    field by field; log and handover records are matched on their text. Every
    match is whole-token, so user_11 never matches user_110.
    """
    pattern = identifier_pattern(frozenset(i.lower() for i in identifiers if i))
    return pattern is not None and pattern.search(record_match_text(record)) is not None


@functools.lru_cache(maxsize=4096)
def identifier_pattern(identifiers):
    """One whole-token regex for a subject's identifiers (same boundaries as contains_identifier)."""
    if not identifiers:
        return None
    alternation = "|".join(re.escape(i) for i in sorted(identifiers, key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9._%+-])(?:" + alternation + r")(?![a-z0-9])")


def record_match_text(record):
    """Lower-cased searchable text of a record, cached on the record.

    Structured records contribute one field value per line, so a token never spans
    two fields; log and handover records contribute their text."""
    cached = record.get("_match_text")
    if cached is None:
        rec = record["record"]
        if isinstance(rec, dict) and list(rec.keys()) != ["raw"] and "observed_state" not in rec:
            cached = "\n".join(str(v) for _, v in iter_fields(rec) if v is not None)
        else:
            raw = rec.get("raw") if isinstance(rec, dict) else None
            cached = (record.get("text") or "") + "\n" + (raw if isinstance(raw, str) else "")
        cached = cached.lower()
        record["_match_text"] = cached
    return cached


OPERATION_ORDER = {
    "FINDING": [
        "REPORT_REMAINING_RECORD",
        "REPORT_EXPOSED_PII",
        "REPORT_ACTIVE_DATA_FLOW",
        "REPORT_RESTORE_RISK",
    ],
    "REMEDIATION": [
        "DISABLE_DATA_FLOW",
        "BLOCK_RESTORE",
        "DELETE_RECORD",
        "REDACT_RECORD",
        "ANONYMIZE_RECORD",
    ],
    "VERIFICATION": [
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_PRESENT",
        "VERIFY_RECORD_REDACTED",
        "VERIFY_RECORD_ANONYMIZED",
        "VERIFY_DATA_FLOW_DISABLED",
        "VERIFY_RESTORE_BLOCKED",
    ],
}


def audit_case(case, identities, audit_log, rules_rows, evidence_records, system_activity, id_to_user, user_ids):
    case_id = case["case_id"].strip()
    email = case.get("email", "").strip()
    audit_at = parse_ts(case.get("audit_at", ""))
    request_text = case.get("request_text", "")

    if not is_deletion_request(request_text):
        return case_id, "NOT_APPLICABLE", []

    user_id, identifiers = resolve_subject(email, id_to_user, audit_log, identities)
    coverage = [r for r in evidence_records if r.get("kind") == "coverage"]
    relevant = find_relevant_records(email, identifiers, audit_at, evidence_records, system_activity)
    observations = latest_observations(relevant, coverage, audit_at)

    findings = []
    remediations = []
    verifications = []

    def report(finding_op, finding_state, fix_op, fix_state, verify_op, verify_state):
        findings.append(mk("FINDING", finding_op, system, resource_id, finding_state, evidence_id))
        remediations.append(mk("REMEDIATION", fix_op, system, resource_id, fix_state, ""))
        verifications.append(mk("VERIFICATION", verify_op, system, resource_id, verify_state, ""))

    def verified(verify_op, verify_state):
        verifications.append(mk("VERIFICATION", verify_op, system, resource_id, verify_state, evidence_id))

    for (system, resource_id), rec in sorted(observations.items()):
        if not resource_id or not resource_id.strip():
            continue
        category = category_for_record(rec, rules_rows)
        evidence_id = rec["file_id"]
        present = record_present(rec)

        if category == "DATA_FLOW":
            enabled = flow_enabled(rec)
            if enabled is None:
                enabled = present
            if enabled:
                report("REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "DISABLE_DATA_FLOW", "DISABLED",
                       "VERIFY_DATA_FLOW_DISABLED", "DISABLED")
            else:
                verified("VERIFY_DATA_FLOW_DISABLED", "DISABLED")
        elif category == "BACKUP_ARCHIVE":
            allowed = restore_allowed(rec)
            if allowed is None:
                allowed = present
            if allowed:
                report("REPORT_RESTORE_RISK", "ACTIVE", "BLOCK_RESTORE", "BLOCKED",
                       "VERIFY_RESTORE_BLOCKED", "BLOCKED")
            else:
                verified("VERIFY_RESTORE_BLOCKED", "BLOCKED")
        elif category in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
            # Retained by policy; only an observed copy can prove it is still available
            if present:
                verified("VERIFY_RECORD_PRESENT", "PRESENT")
        elif category == "ACCOUNT_TOMBSTONE" and handover_state(rec) != "NOT_FOUND":
            # A tombstone is marked DELETED by design; it must keep only the stable reference
            if has_direct_identifiers(rec):
                report("REPORT_EXPOSED_PII", "PRESENT", "REDACT_RECORD", "REDACTED",
                       "VERIFY_RECORD_REDACTED", "REDACTED")
            else:
                verified("VERIFY_RECORD_REDACTED", "REDACTED")
        elif not present:
            verified("VERIFY_RECORD_ABSENT", "ABSENT")
        else:
            action = required_action_for(category, "", rec, rules_rows)
            exposed = identifier_exposure(rec)
            if action in ("REDACT", "ANONYMIZE") and exposed is None:
                # The copy remains but its fields are not visible, so treatment cannot be
                # verified; report the remaining record and remove it.
                report("REPORT_REMAINING_RECORD", "PRESENT", "DELETE_RECORD", "ABSENT",
                       "VERIFY_RECORD_ABSENT", "ABSENT")
            elif action == "REDACT":
                if exposed:
                    report("REPORT_EXPOSED_PII", "PRESENT", "REDACT_RECORD", "REDACTED",
                           "VERIFY_RECORD_REDACTED", "REDACTED")
                else:
                    verified("VERIFY_RECORD_REDACTED", "REDACTED")
            elif action == "ANONYMIZE":
                if exposed:
                    report("REPORT_EXPOSED_PII", "PRESENT", "ANONYMIZE_RECORD", "ANONYMIZED",
                           "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED")
                else:
                    verified("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED")
            elif action == "RETAIN":
                verified("VERIFY_RECORD_PRESENT", "PRESENT")
            else:
                report("REPORT_REMAINING_RECORD", "PRESENT", "DELETE_RECORD", "ABSENT",
                       "VERIFY_RECORD_ABSENT", "ABSENT")

    # A retained audit record alone does not show that the subject's data was treated:
    # COMPLETE needs evidence for at least one copy outside the security audit log.
    proves_treatment = any(v["system_id"] != "security_service" for v in verifications)
    if not findings and proves_treatment:
        return case_id, "COMPLETE", build_steps([], [], verifications)

    # INCOMPLETE: findings cite evidence; remediation and verification describe future work.
    # With no finding supported by admissible evidence, the steps stay limited to the
    # verifications that must hold, rather than inventing a finding.
    for s in verifications:
        s["evidence_id"] = ""
    return case_id, "INCOMPLETE", build_steps(findings, remediations, verifications)


def main():
    identities = load_csv("user_identities.csv")
    audit_log = load_csv("user_audit_log.csv")
    rules_rows = load_csv("deletion_rules.csv")
    evidence_manifest = load_csv("evidence_files.csv")
    requests = load_csv("deletion_requests.csv")
    system_activity = load_csv("system_activity.csv")

    id_to_user, user_ids = build_identity_index(identities, audit_log)
    evidence_records = load_all_evidence(evidence_manifest)

    out_rows = []
    for case in requests:
        case_id, status, steps = audit_case(
            case, identities, audit_log, rules_rows, evidence_records,
            system_activity, id_to_user, user_ids,
        )
        steps_json = json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
        out_rows.append({"case_id": case_id, "deletion_status": status, "steps_json": steps_json})

    out_rows.sort(key=lambda r: r["case_id"])

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        writer.writeheader()
        for r in out_rows:
            writer.writerow(r)

    print(f"Wrote {len(out_rows)} rows to {OUTPUT}")


if __name__ == "__main__":
    main()