# Architecture

This document describes the implementation in `code/main.py` as it is. The program is a single-process, deterministic rule engine. It uses only the Python standard library, plus the optional poppler `pdftotext` command for PDFs. There is no language-model or network call at run time.

## Data flow

```text
dataset/*.csv ──► load_csv
                    │
user_identities.csv + user_audit_log.csv ──► build_identity_index ──► id_to_user
evidence_files.csv + dataset/evidence/** ──► load_all_evidence ──► evidence records + snapshot coverage
deletion_requests.csv ──► for each case: audit_case
                              ├─ is_deletion_request ──(no)──► NOT_APPLICABLE, []
                              ├─ resolve_subject ──► identifier set
                              ├─ find_relevant_records (audit_at filter, matching, catalog links)
                              ├─ latest_observations (per store, snapshot absence)
                              ├─ category → deletion_rules action → steps
                              └─ build_steps (ordering, numbering)
                         ──► output.csv (sorted by case_id)
```

## Modules (functions in `code/main.py`)

### Request classification
`is_deletion_request` matches deletion stems (`delet`, `remov`, `eras…`, `purg`, `destroy`) and terms in Spanish, Portuguese, French, German, Italian, Japanese, Korean and Chinese. It also accepts "close" when it refers to the account or personal data, and deletion-verification phrases such as "profile is gone". Negations are tied to deletion verbs ("do not delete", "not a request to erase", "without removing").

### Identity resolution
- `build_identity_index` maps current emails, historical emails (`user_audit_log.csv`, `field_name = email`, `old_value`/`new_value`) and user_ids to the stable `user_id`.
- `resolve_subject` returns that `user_id` together with every current and historical email, phone and email hash.

### Evidence parsing
`load_all_evidence` reads the manifest and dispatches each file by extension. Each parsed record carries `file_id`, the manifest `captured_at`, `system`, `store`, `resource_id`, the raw `record` and its `text`.

| Format | Parser | Notes |
| --- | --- | --- |
| `.sql` | `parse_sql_file` | Executed in in-memory SQLite. Each row becomes a record; JSON payload columns are read by `iter_fields`. Emits a coverage record per file: the (system, store) pairs the file fully lists, limited to `snapshot_scope` when that table exists. |
| `.json` | `parse_json_file` | Recursive walk; every object with a `resource_id` becomes a record. JSON bundles are treated as extracts and never prove absence. |
| `.log` | `normalize_log_line`, `parse_log_file` | Six layouts: `key=value`; `[LEVEL] [service] [trace]`; JSON-lines; pipe-delimited; RFC 5424 syslog; `requestId=… function=… message="…"`. Lines are grouped by trace and split per resource. Resource-less lines of a trace (e.g. the lookup by email) are shared with each of its resources. |
| `.txt` | `parse_text_file` | The pipe-delimited handover table (`system | resource | CATEGORY | STATE | identifier`); other text falls back to log parsing. |
| `.pdf` | `parse_pdf_file` | Uses `pdftotext` if it is on `PATH`. A document is used only when it names exactly one resource, either directly or through a business label that a `LINK_RESOURCE_IDENTITY` event maps to a resource (`catalog_labels`). |
| `.png` | — | Not parsed (no OCR). |

`canonical_system` maps resource prefixes to the system used in the output (for example `support_` → `support_platform`). The originating table or log service is kept as `store` (`store_of`): names containing "replica" are separate copies; everything else is the primary store.

### Matching
`find_relevant_records` keeps a record when all of the following hold:
- its file was captured at or before `audit_at`;
- its own `recorded_at`/`observed_at`, if present, is at or before `audit_at` (`observation_time`);
- it contains one of the subject's identifiers as a whole token (`identifier_pattern`, `record_match_text`), or its resource was linked to the subject by a `LINK_RESOURCE_IDENTITY` event (`linked_resources`).

No other `system_activity` events are used: their timing does not identify a single subject.

### State normalization
- `lookup_report` reads explicit per-resource results: handover rows, and `record_type` + `result` lookups in JSON or logs (FOUND, NOT_FOUND, IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE, DISABLED, BLOCKED).
- `record_present` uses the lookup state first. Otherwise it takes the last decisive event in a log group (`text_presence`: 404 or `rows=0` absent; 200, `rows≥1` or `hit` present; cache lookups and 5xx ignored), then structured fields (`status_code`, `rows`, `profile: null`, `account_status`).
- `flow_enabled` and `restore_allowed` read `enabled` / `restore_allowed` flags (`true`/`false`, `1`/`0`) or handover states, and return `None` when the evidence is silent.
- `identifier_exposure` returns True when a direct identifier (name, email, phone, email hash, requester/owner/customer contact, PII-scanner `matched_value`) is visible. It returns False when such fields are shown cleared, and None when none are shown.

### State at audit_at
`latest_observations` takes, per (system, resource) and per store, the latest admissible observation. A later covering SQL snapshot that no longer lists the resource counts as an absent observation; this is never applied to `security_service`. If any store's latest observation still holds the record, the record remains.

### Policy and step generation (`audit_case`)

| Category | Evidence state | Steps |
| --- | --- | --- |
| DATA_FLOW | enabled | REPORT_ACTIVE_DATA_FLOW → DISABLE_DATA_FLOW → VERIFY_DATA_FLOW_DISABLED |
| DATA_FLOW | disabled | VERIFY_DATA_FLOW_DISABLED (cited) |
| BACKUP_ARCHIVE | restore allowed | REPORT_RESTORE_RISK → BLOCK_RESTORE → VERIFY_RESTORE_BLOCKED |
| BACKUP_ARCHIVE | blocked | VERIFY_RESTORE_BLOCKED (cited) |
| SECURITY_AUDIT_LOG, LEGAL_HOLD_RECORD | present | VERIFY_RECORD_PRESENT (cited) |
| ACCOUNT_TOMBSTONE | direct identifiers present | REPORT_EXPOSED_PII → REDACT_RECORD → VERIFY_RECORD_REDACTED |
| ACCOUNT_TOMBSTONE | direct identifiers cleared | VERIFY_RECORD_REDACTED (cited) |
| any other | absent | VERIFY_RECORD_ABSENT (cited) |
| REDACT / ANONYMIZE rule | exposed | REPORT_EXPOSED_PII → REDACT_RECORD / ANONYMIZE_RECORD → matching verify |
| REDACT / ANONYMIZE rule | cleared | VERIFY_RECORD_REDACTED / VERIFY_RECORD_ANONYMIZED (cited) |
| REDACT / ANONYMIZE rule | fields not visible | REPORT_REMAINING_RECORD → DELETE_RECORD → VERIFY_RECORD_ABSENT |
| DELETE rule | present | REPORT_REMAINING_RECORD → DELETE_RECORD → VERIFY_RECORD_ABSENT |

Status:
- **COMPLETE** when there are no findings and at least one verification outside `security_service`; steps are verification only, with evidence.
- **INCOMPLETE** otherwise; findings keep their evidence, and remediation and verification steps use an empty `evidence_id`.

`build_steps` orders steps by phase, by the operation order in the problem statement, then by `system_id` and `resource_id`, and numbers them from 1.

## Guarantees checked on the final output

The final `output.csv` was checked for all of the following:
- 518 sorted rows;
- valid step schema and operation/state pairs;
- consecutive numbering and phase order;
- no cited evidence after `audit_at`;
- every citation names a file containing the cited resource (or a covering SQL snapshot for absence).

## Known gaps

- PNG evidence is unread, and PDFs require `pdftotext`.
- Resources whose only link to the subject is `system_activity` timing are not used; several gold sample verifications depend on such resources.
- Cases with no admissible evidence produce INCOMPLETE rows with empty or verification-only steps rather than invented findings.
