"""Evidence parsers.

load_all_evidence() reads evidence_files.csv and turns every file into evidence
records (see state.py for the record shape). SQL snapshots additionally emit one
"coverage" record listing the (system, store) pairs the snapshot fully captures,
which is what allows absence from a snapshot to prove a deletion.
"""

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .io_utils import DATASET_DIR, parse_ts

PRIMARY_STORE = "primary"
UNKNOWN_SYSTEM = "unknown_service"

# Resource-id prefixes name the owning system used in the output; replica or store
# names such as support_replica or customer_profiles_failover_replica map back to it.
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

RESOURCE_ID_PATTERN = re.compile(
    r"\b(?:audit_record|tombstone|data_flow|account|analytics|search|support|export|backup|marketing)_\d+\b"
)

HANDOVER_LINE = re.compile(
    r"^-\s*(\S+)\s*\|\s*([a-z_]+)\s*\|\s*([A-Za-z0-9_]+)\s*\|\s*([A-Z_]+)\s*\|\s*([A-Z_]+)\s*\|\s*(.+?)\s*$"
)


# --------------------------------------------------------------------------- helpers

def canonical_system(resource_id, fallback):
    rid = (resource_id or "").lower()
    for prefix, system in RESOURCE_SYSTEMS:
        if rid.startswith(prefix):
            return system
    return fallback


def store_of(name):
    """Replica tables/services are separate copies; everything else is the primary store."""
    name = (name or "").lower()
    return name if "replica" in name else PRIMARY_STORE


def system_from_table(table_name):
    """System owning a typed SQL table, e.g. customer_profiles -> account_service."""
    t = table_name.lower()
    if "backup" in t or "snapshot" in t:
        return "backup_service"
    if "analytics" in t:  # before "profile": customer_analytics_profiles is analytics
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
    return UNKNOWN_SYSTEM


def evidence_record(file_id, captured_at, system, resource_id, record, text, **extra):
    return {"file_id": file_id, "captured_at": captured_at, "system": system,
            "resource_id": resource_id, "record": record, "text": text, **extra}


def coverage_record(file_id, captured_at, stores, resource_ids):
    """A snapshot that lists every record of the given (system, store) pairs."""
    return {"kind": "coverage", "file_id": file_id, "captured_at": captured_at,
            "stores": set(stores), "resource_ids": {r for r in resource_ids if r}}


def catalog_labels(system_activity):
    """Map business labels to resource ids from data-catalog links, e.g.
    'linked export_1842 (NST-2026-0718-1842) to ...' -> {'NST-2026-0718-1842': 'export_1842'}."""
    labels = {}
    for row in system_activity:
        if row.get("event_type") != "LINK_RESOURCE_IDENTITY":
            continue
        m = re.search(r"(?:linked|confirmed) (\S+) \(([^)]+)\)", row.get("details", ""))
        if m and m.group(2) != m.group(1) and not m.group(2).startswith("user_"):
            labels[m.group(2)] = m.group(1)
    return labels


# --------------------------------------------------------------------------- parsers

def parse_sql_file(path, file_id, captured_at):
    """One record per table row, plus a coverage record for the snapshot.

    Typed tables (customer_profiles, ...) cover their store even when empty. A declared
    snapshot_scope table limits coverage to the systems it lists: empty typed tables
    outside that scope were not captured, not emptied."""
    records, covered, present_ids, scope = [], set(), set(), None
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(Path(path).read_text(encoding="utf-8", errors="replace"))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            try:
                cur = conn.execute(f'SELECT * FROM "{table}"')
            except sqlite3.Error:
                continue
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if table == "snapshot_scope":
                scope = {r[0] for r in rows if r and r[0]}
                covered.update((s, PRIMARY_STORE) for s in scope)
                continue
            store = store_of(table)
            if "origin_system" not in columns:
                covered.add((system_from_table(table), store))
            for row in rows:
                rec = dict(zip(columns, row))
                system = rec.get("origin_system") or rec.get("system") or system_from_table(table)
                resource_id = rec.get("resource_id") or rec.get("id") or ""
                present_ids.add(resource_id)
                covered.add((canonical_system(resource_id, system), store))
                records.append(evidence_record(file_id, captured_at, system, resource_id, rec,
                                               json.dumps(rec, ensure_ascii=False), store=store, table=table))
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()
    covered = {(s, st) for s, st in covered if s != UNKNOWN_SYSTEM and (scope is None or s in scope)}
    if covered:
        records.append(coverage_record(file_id, captured_at, covered, present_ids))
    return records


def parse_json_file(path, file_id, captured_at):
    """Every JSON object carrying a resource_id becomes a record.

    JSON bundles are extracts, not full store dumps, so omission from them never
    proves absence (no coverage record is emitted)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError):
        return []
    records = []

    def walk(node):
        if isinstance(node, dict):
            if "resource_id" in node:
                system = node.get("system_id") or node.get("service") or UNKNOWN_SYSTEM
                resource_id = node.get("resource_id") or node.get("id") or ""
                records.append(evidence_record(file_id, captured_at, system, resource_id, node,
                                               json.dumps(node, ensure_ascii=False)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return records


def normalize_log_line(line):
    """Rewrite the supported layouts as 'ts service=... trace_id=... key="value" ...'.

    Layouts: key=value lines; '[LEVEL] [service] [trace]' lines (read directly by
    parse_log_file); JSON-lines 'ts {...}'; pipe 'ts | LEVEL | service | trace | event | ...';
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
        ts, level, service, trace, event, rest = m.groups()
        return f"{ts} level={level} service={service} trace_id={trace} event={event} {rest}"
    m = re.match(r"^(\S+) <\d+>\d+ \S+ ([a-z_]+) (\S+) - - (.*)$", line)
    if m:
        ts, service, trace, rest = m.groups()
        return f"{ts} service={service} trace_id={trace} {rest}"
    m = re.match(r'^(\S+) requestId=(\S+) (.*?)function=([a-z_]+) message="(.*)"\s*$', line)
    if m:
        ts, trace, prefix, service, message = m.groups()
        return f"{ts} trace_id={trace} service={service} {prefix}{message}"
    return line


def parse_log_file(path, file_id, captured_at):
    """One record per (trace, resource).

    Lines of a trace that name no resource (e.g. the lookup by email) are shared by
    every resource of that trace; lines naming a different resource are not, so one
    resource's result is never read as another's."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    traces = {}  # insertion-ordered: key -> [(line, system, resource_id)]
    for raw_line in lines:
        line = normalize_log_line(raw_line)
        if not line.strip():
            continue
        m_sys = re.search(r'\bservice="?([a-z_]+)', line) or re.search(r"\] \[([a-z_]+)\] \[", line)
        m_res = re.search(r'resource_id="?([A-Za-z0-9_]+)', line)
        m_trace = re.search(r'trace_id="?([\w.-]+)', line) or re.search(r"\[(tr-[^\]]+)\]", line)
        system = m_sys.group(1) if m_sys else UNKNOWN_SYSTEM
        resource_id = m_res.group(1) if m_res else ""
        key = m_trace.group(1) if m_trace else (resource_id or f"__line__{len(traces)}")
        traces.setdefault(key, []).append((line, system, resource_id))

    records = []
    for entries in traces.values():
        resources = list(dict.fromkeys(r for _, _, r in entries if r)) or [""]
        for resource_id in resources:
            own = [(line, system) for line, system, r in entries if r in ("", resource_id)]
            systems = ([s for _, s, r in entries if r == resource_id and s != UNKNOWN_SYSTEM]
                       or [s for _, s in own if s != UNKNOWN_SYSTEM] or [UNKNOWN_SYSTEM])
            text = "\n".join(line for line, _ in own)
            records.append(evidence_record(file_id, captured_at, systems[-1], resource_id,
                                           {"raw": text}, text))
    return records


def parse_text_file(path, file_id, captured_at):
    """Handover rows '- ts | system | resource | CATEGORY | STATE | identifier';
    other text files are parsed as logs."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        m = HANDOVER_LINE.match(line.strip())
        if m:
            _, system, resource_id, category, state, identifier = m.groups()
            row = {"data_category": category, "observed_state": state, "identifier": identifier}
            records.append(evidence_record(file_id, captured_at, system, resource_id, row, line.strip()))
    return records or parse_log_file(path, file_id, captured_at)


def parse_pdf_file(path, file_id, captured_at, labels=None):
    """Extract PDF text with poppler's pdftotext when it is installed (optional).

    A document is used only when it names exactly one resource, by id or by a catalog
    label, so a multi-subject register cannot link unrelated people to a resource."""
    exe = shutil.which("pdftotext")
    if not exe:
        return []
    try:
        result = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    text = result.stdout.decode("utf-8", errors="replace")
    resource_ids = set(RESOURCE_ID_PATTERN.findall(text))
    if not resource_ids and labels:
        resource_ids = {rid for label, rid in labels.items() if label in text}
    if len(resource_ids) != 1:
        return []
    return [evidence_record(file_id, captured_at, UNKNOWN_SYSTEM, resource_ids.pop(), {"raw": text}, text)]


# --------------------------------------------------------------------------- loader

def parse_evidence_file(file_id, rel_path, captured_at, labels=None, dataset_dir=DATASET_DIR):
    path = Path(dataset_dir) / rel_path.replace("\\", "/")
    suffix = path.suffix.lower()
    if suffix == ".sql":
        return parse_sql_file(path, file_id, captured_at)
    if suffix == ".json":
        return parse_json_file(path, file_id, captured_at)
    if suffix == ".txt":
        return parse_text_file(path, file_id, captured_at)
    if suffix == ".pdf":
        return parse_pdf_file(path, file_id, captured_at, labels)
    if suffix == ".log":
        return parse_log_file(path, file_id, captured_at)
    return []  # PNG screenshots are not parsed (no OCR)


def load_all_evidence(evidence_manifest, system_activity=(), dataset_dir=DATASET_DIR):
    """Parse every manifest file. captured_at comes from the manifest, which governs
    admissibility. Each record's system is canonicalized and its store recorded."""
    labels = catalog_labels(system_activity)
    records = []
    for row in evidence_manifest:
        captured_at = parse_ts(row.get("captured_at", ""))
        for rec in parse_evidence_file(row["file_id"].strip(), row["file_path"].strip(),
                                       captured_at, labels, dataset_dir):
            if rec.get("kind") != "coverage":
                rec.setdefault("store", store_of(rec["system"]))
                rec["system"] = canonical_system(rec["resource_id"], rec["system"])
            records.append(rec)
    return records
