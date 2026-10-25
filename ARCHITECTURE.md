# Architecture

The agent is a single-process, deterministic rule engine. It uses the Python standard library only, plus the optional poppler `pdftotext` command for PDFs. It makes no language-model or network calls. This document describes the code in `code/ghostaudit/` as implemented.

## Pipeline

```text
                         ┌──────────────── evidence_files.csv + dataset/evidence/** ──────────────┐
                         ▼                                                                         │
user_identities.csv ─► identity.build_identity_index        evidence.load_all_evidence ◄── system_activity.csv
user_audit_log.csv  ─┘            │                                   │  (records + snapshot coverage)
                                  ▼                                   ▼
deletion_requests.csv ─► audit.audit_case ── for each request ─────────────────────────────────────────┐
                           1. classifier.is_deletion_request ── no ─► NOT_APPLICABLE, []                │
                           2. identity.resolve_subject ─► identifier set                                │
                           3. audit.find_relevant_records  (audit_at filter, matching, catalog links)  │
                           4. audit.latest_observations    (per store, snapshot absence)               │
                           5. policy + audit.plan_resource (deletion_rules.csv → steps)                │
                           6. StepPlan.ordered             (ordering, numbering, status)                │
                                                                                                        ▼
                                                                     main.run ─► output.csv (sorted by case_id)
```

`code/main.py` loads every input once into an `AuditContext`, audits each request and writes the CSV. `code/evaluate.py` reuses the same context to score the public samples and validate `output.csv`.

## Modules

| Module | Responsibility |
| --- | --- |
| `io_utils` | Dataset paths, CSV loading, ISO-8601 parsing |
| `classifier` | Does the request text authorize deletion? |
| `identity` | Identity index, subject resolution, whole-token identifier matching |
| `linkage` | Links deletion-workflow events in `system_activity.csv` to the request they served |
| `evidence` | Parsers for every evidence format; canonical systems and stores; snapshot coverage |
| `state` | Presence, explicit lookup results, connector and restore flags, identifier exposure, observation time |
| `policy` | Data category of a record; the action `deletion_rules.csv` requires |
| `audit` | Evidence selection, state at `audit_at`, step planning, status decision |
| `validation` | Output contract checks and sample scoring (used by `evaluate.py` and the tests) |

## Evidence records

Every parser emits records of the same shape:

| Key | Meaning |
| --- | --- |
| `file_id`, `captured_at` | Manifest id and capture time (the capture time governs admissibility) |
| `system` | Canonical system, derived from the resource-id prefix (e.g. `support_` → `support_platform`) |
| `store` | `primary`, or the replica table/service name (e.g. `support_replica`) |
| `resource_id` | The resource the record describes |
| `record` | Parsed content: a structured dict, `{"raw": text}`, or a lookup row `{data_category, observed_state, identifier}` |
| `text` | Searchable text |

SQL snapshots also emit a **coverage** record. It lists the (system, store) pairs the snapshot fully captures, limited to `snapshot_scope` when that table exists.

| Format | Parser | Notes |
| --- | --- | --- |
| SQL | `parse_sql_file` | In-memory SQLite. Rows, including JSON payload columns, become records. Typed tables cover their store even when empty. |
| JSON | `parse_json_file` | Every object with a `resource_id`. Bundles are extracts, so omission never proves absence. |
| Logs | `normalize_log_line`, `parse_log_file` | Six layouts: `key=value`, `[LEVEL] [service] [trace]`, JSON-lines, pipe-delimited, RFC 5424 syslog, `requestId=… message="…"`. Lines are grouped by trace and split per resource; a trace's resource-less lines (e.g. the lookup by email) are shared with each of its resources. |
| Handover text | `parse_text_file` | `- ts \| system \| resource \| CATEGORY \| STATE \| identifier` rows. |
| PDF | `parse_pdf_file` | Via `pdftotext`. Used only when the document names exactly one resource, by id or by a business label that a `LINK_RESOURCE_IDENTITY` event maps to a resource (`catalog_labels`). |
| PNG | — | Not parsed. |

## Decision rules

**Identity.** The request email may be current or historical. `user_audit_log.csv` (`field_name`, `old_value`, `new_value`) supplies former emails, phones and hashes. A record matches when it contains one of these as a whole token, when its resource was linked to the subject by a `LINK_RESOURCE_IDENTITY` event at or before `audit_at`, or when the workflow linkage assigns the resource to the request.

**Workflow linkage** (`linkage.activity_links`). Deletion-worker events name the resources they acted on but not the person. Over all requests together, an event is a candidate for a request when:

- it falls within the request's window around `reported_at` (account deletions 1.5 h before to 15 min after; other actions 1.5 h before to 2 h after) and not after `audit_at`;
- for account deletions, its result is consistent with the reported status (COMPLETE → SUCCESS, NOT_STARTED → FAILED, PARTIALLY_COMPLETE → PARTIAL or SUCCESS);
- no evidence ties the resource to a different person.

Each request takes at most one resource per system and each resource serves one request; a link is assigned only when a single candidate remains, iterated to a fixed point. On the public samples this gives 73 correct and 0 wrong links. Event results are claims, so a linked resource's state is still read from evidence; a linked resource with no evidence row is proven absent only by a covering SQL snapshot.

**Admissibility.** A record is used only if its file was captured at or before `audit_at`, and its own `recorded_at`/`observed_at`, when present, is too.

**State.**
- `lookup_report` reads explicit results: FOUND, NOT_FOUND, IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE, DISABLED, BLOCKED.
- Otherwise the last decisive log event decides: 404 or `rows=0` mean absent; 200, `rows≥1` or `hit` mean present. Cache lookups and 5xx are ignored.
- Otherwise structured fields decide (`status_code`, `rows`, `profile: null`, `account_status`).
- Missing or ambiguous evidence is never read as deletion.
- `identifier_exposure` is three-valued: identifier visible, shown cleared, or not visible.

**State at `audit_at`.** Per (system, resource), each store is judged by its latest admissible observation. A later SQL snapshot that covers the store and no longer lists the resource counts as an absent observation. This is never applied to the retained `security_service`. The resource remains if any store's latest observation still holds it.

**Policy and steps.**

| Category | Evidence state | Steps |
| --- | --- | --- |
| DATA_FLOW | enabled | REPORT_ACTIVE_DATA_FLOW → DISABLE_DATA_FLOW → VERIFY_DATA_FLOW_DISABLED |
| DATA_FLOW | disabled | VERIFY_DATA_FLOW_DISABLED (cited) |
| BACKUP_ARCHIVE | restore allowed | REPORT_RESTORE_RISK → BLOCK_RESTORE → VERIFY_RESTORE_BLOCKED |
| BACKUP_ARCHIVE | blocked | VERIFY_RESTORE_BLOCKED (cited) |
| SECURITY_AUDIT_LOG, LEGAL_HOLD_RECORD | present | VERIFY_RECORD_PRESENT (cited) |
| ACCOUNT_TOMBSTONE | direct identifiers present | REPORT_EXPOSED_PII → REDACT_RECORD → VERIFY_RECORD_REDACTED |
| ACCOUNT_TOMBSTONE | only stable reference | VERIFY_RECORD_REDACTED (cited) |
| any other | absent | VERIFY_RECORD_ABSENT (cited) |
| REDACT / ANONYMIZE rule | exposed | REPORT_EXPOSED_PII → REDACT_RECORD / ANONYMIZE_RECORD → matching verification |
| REDACT / ANONYMIZE rule | cleared | VERIFY_RECORD_REDACTED / VERIFY_RECORD_ANONYMIZED (cited) |
| REDACT / ANONYMIZE rule | fields not visible | REPORT_REMAINING_RECORD → DELETE_RECORD → VERIFY_RECORD_ABSENT |
| DELETE rule | present | REPORT_REMAINING_RECORD → DELETE_RECORD → VERIFY_RECORD_ABSENT |

**Status.**
- **COMPLETE** requires no findings and at least one verification proven by admissible evidence (a retained audit record that is still present counts, as in the public sample answers). Its steps are verification only, each citing evidence.
- **INCOMPLETE** when a violation is found, or when no admissible evidence links any record to the subject. Findings cite evidence; remediation and verification steps use an empty `evidence_id`.

Steps are ordered by phase, by the operation order in the problem statement, then by `system_id` and `resource_id`, and numbered from 1.

## Quality gates

- `python -m unittest` runs 43 tests: classifier, identity, every evidence format, state rules, step ordering, end-to-end case decisions, and the output contract.
- `python code/evaluate.py` scores the 32 public samples and validates `output.csv`. It exits non-zero on any contract violation.
- The run is deterministic: repeated runs produce a byte-identical `output.csv`.

## Known gaps

- PNG evidence is unread, and PDFs require `pdftotext`.
- Workflow events that remain ambiguous after the linkage constraints are not used; a few sample verifications (mostly analytics) depend on them.
- Cases without any admissible evidence linking a record to the subject produce INCOMPLETE rows with no steps, rather than invented findings.
