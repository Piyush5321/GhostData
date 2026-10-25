# Evaluation Report — Ghost Data Still Exists

The agent is a deterministic, rule-based Python program (`code/main.py`, package `code/ghostaudit/`). It makes no model or API calls. Every number below was produced by `python code/evaluate.py` on the final code, using participant-visible files only.

## How accuracy was measured

The only labelled data is `dataset/sample_deletion_requests.csv` (32 cases) with `dataset/sample_output.csv`. `code/evaluate.py` runs each sample case through `audit_case()` and compares the result with the published answer (`ghostaudit.validation.score_against_gold`).

- **Status accuracy**: share of cases whose `deletion_status` matches.
- **Step-level accuracy**: precision and recall of steps, where a step matches when `phase`, `system_id`, `resource_id`, `operation`, `state` and `evidence_id` are all equal.
- **Field-level accuracy**: steps aligned by position; each of the 7 fields is compared; denominator = 7 × max(len(gold), len(predicted)) per case.
- **Sequence accuracy**: share of cases whose ordered step list matches exactly.

There are no labels for the 518 evaluation cases, so their correctness is checked structurally and by tracing every cited evidence file (see "Output validation").

## Results on the 32 sample cases

| Metric | Value |
| --- | --- |
| Status accuracy | 31 / 32 (96.9%) |
| Sequence accuracy (exact ordered steps) | 16 / 32 (50.0%) |
| Step-level precision / recall | 0.989 / 0.902 |
| Field-level accuracy (position-aligned) | 0.835 |

Status confusion (gold → predicted): COMPLETE → COMPLETE 9, COMPLETE → INCOMPLETE 1, INCOMPLETE → INCOMPLETE 22. No INCOMPLETE case was predicted COMPLETE, and no case was misclassified as NOT_APPLICABLE.

| Phase | True positives | False positives | False negatives | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| FINDING | 41 | 0 | 0 | 1.000 | 1.000 |
| REMEDIATION | 41 | 0 | 0 | 1.000 | 1.000 |
| VERIFICATION | 92 | 2 | 19 | 0.979 | 0.829 |

Per-field agreement (194 aligned positions): step_no 0.902, phase 0.902, system_id 0.778, resource_id 0.778, operation 0.794, state 0.794, evidence_id 0.897.

### Why the remaining samples differ

All remaining differences are verification steps:

- **Retained audit records with no evidence** (e.g. `audit_record_5569` for case_20) appear in no evidence file, so they cannot be cited.
- **Ambiguous workflow events.** Several analytics anonymizations (e.g. `analytics_1895` for case_25 and `analytics_4566` for case_26) are reported by the deletion worker at the same moment for different requests with the same reported status. The linkage declines to guess, so they stay unlinked.
- **case_04**, the one status miss: its account has no workflow event and no evidence row, and its analytics event is ambiguous.
- **2 extra verification steps** concern resources that evidence genuinely links to the subject but the gold answer omits (e.g. `account_6523` for case_17, an earlier lookup by the subject's own email).

## Additional evaluation cases (created by the team)

The suite in `code/tests/` (43 tests) contains these cases. Each one reproduces a situation found in the real data. All pass.

| # | Case | Input | Expected | Test |
| --- | --- | --- | --- | --- |
| 1 | NOT_FOUND lookup (evidence_13, case_133) | JSON record `result: "NOT_FOUND"`, `record_type: USER_PROFILE` | absent, category USER_PROFILE | `test_state.PresenceTest.test_lookup_results_in_json` |
| 2 | Cache miss inside a trace | `cache_lookup result=miss` followed by a `rows=1` PII scan | present (a cache miss is not absence) | `test_state.PresenceTest.test_cache_miss_does_not_mean_absent` |
| 3 | Replica drift (case_05) | replica row present on 09-02, primary readback 404 on 09-05 | replica observation decides: remaining | `test_audit.LatestObservationTest.test_replica_copy_survives_later_primary_readback` |
| 4 | Historical email (case_12) | request from an address found only in `user_audit_log.old_value` | resolves to the stable user and all identifiers | `test_identity.IdentityResolutionTest.test_historical_email_resolves_to_stable_user` |
| 5 | Record dated after audit_at (case_135) | admissible file listing a record with `recorded_at` after `audit_at` | record ignored | `test_audit.AdmissibilityTest.test_record_observed_after_audit_at_is_excluded` |
| 6 | Scoped snapshot (evidence_90) | `snapshot_scope = account_service`; empty tables for other systems | coverage only for `account_service` | `test_evidence.SqlSnapshotTest.test_snapshot_scope_limits_coverage` |
| 7 | Prefix collision (case_34) | subject `user_11`, log line about `user_110` | no match | `test_identity.IdentifierMatchingTest.test_whole_token_matching` |
| 8 | Six log layouts | key=value, bracket, `requestId/message=`, JSON-lines, pipe, RFC 5424 syslog | lookup line joined to its resource line by trace | `test_evidence.LogParsingTest` |
| 9 | Multilingual and negated requests | Spanish, Portuguese, French, Japanese, Korean; "not a request to erase it" | deletion vs NOT_APPLICABLE | `test_classifier.ClassifierTest` |
| 10 | Catalog label in a document (evidence_18, case_33) | statement PDF printing `NST-2026-0718-1842` | resolved to `export_1842` | `test_evidence.CatalogLabelTest` |
| 11 | Retained audit record only (case_21, case_25) | only a present security audit record matches the subject; nothing remains | COMPLETE with a cited VERIFY_RECORD_PRESENT | `test_audit.AuditCaseTest.test_retained_audit_record_with_no_violation_is_complete` |
| 12 | Workflow linkage by reported status (case_24 / case_25) | two requests with the same reported_at; one account deletion SUCCESS, one FAILED | each account linked to the request whose reported status matches | `test_linkage.ActivityLinkTest.test_reported_status_disambiguates_account_events` |
| 13 | Ambiguous workflow events | two analytics anonymizations at the same time for two equivalent requests | no link (no guessing) | `test_linkage.ActivityLinkTest.test_ambiguous_candidates_are_not_linked` |
| 14 | Linked account proven absent by snapshot (case_01) | account named only by the workflow; missing from a covering snapshot | VERIFY_RECORD_ABSENT citing the snapshot | `test_linkage.LinkedResourceStateTest` |
| 15 | Output contract | the generated `output.csv` | 518 rows, 0 contract violations | `test_output.OutputContractTest` |

## Failures found and improvements made

| Failure | Effect before fix | Fix |
| --- | --- | --- |
| `"delete"` matched against text, so `"deletion"` and `"deleting"` were missed | about 210 plain deletion requests marked NOT_APPLICABLE | stem patterns plus multilingual terms; negations tied to deletion verbs |
| 29 manifest files under `dataset/evidence/records/` missing on disk | JSON and handover evidence unavailable | restored byte-for-byte from the original challenge archive (all 65 present files verified identical) |
| Historical identifiers read from a non-existent `email` column | former emails, phones and hashes never matched | read `field_name`/`old_value`/`new_value` |
| Substring identifier matching | `user_11` matched `user_110` | whole-token matching |
| System derived from table or log service name | `customer_profiles` → analytics; `support_replica`, `unknown_service` | canonical system from resource prefix; store tracked separately |
| `result: FOUND / NOT_FOUND` not understood | 34 findings reported records that the evidence says are gone | shared lookup-state model for JSON, logs and handover rows |
| Any cache miss in a trace meant "absent" | present records could be verified as absent | presence from the last decisive event; cache lookups and 5xx ignored |
| Four log layouts unparsed (evidence_06, 25, 34, 53); pipe and syslog lines not grouped | 25 authorized cases had no evidence | layout normalization and trace grouping |
| Tombstones with `account_status=DELETED` read as absent | exposed tombstone PII missed | tombstones checked for direct identifiers |
| First record kept instead of latest; replicas ignored | stale or hidden copies | latest observation per store; any store still holding a copy wins |
| Snapshot coverage ignored `snapshot_scope` | potential false absence | coverage limited to declared scope; JSON extracts never prove absence |
| Records with their own timestamp after `audit_at` used | case_135 and case_383 judged on later evidence | per-record `recorded_at`/`observed_at` filter |
| Documents that print a business label instead of a resource id skipped | case_33 had no steps although an admissible statement PDF names the subject | PDF resource resolved through `LINK_RESOURCE_IDENTITY` labels |
| Cases with no violation but only a retained audit record marked INCOMPLETE | 2 public samples (case_21, case_25) and 78 evaluation cases disagreed with the published COMPLETE pattern | COMPLETE when no violation is found and at least one verification is proven by evidence |
| Resources named only by the deletion workflow ignored | accounts and analytics records verified in the sample answers were missing (e.g. `account_001`, `account_3136`) | constrained activity linkage (`ghostaudit.linkage`): timing window, status-consistent result, ownership exclusion and uniqueness; 73 correct and 0 wrong links on the samples; state still decided by evidence only |
| One 1,100-line script and print-only test scripts that could not fail | hard to review; regressions could pass unnoticed | split into the `ghostaudit` package; `unittest` suite with assertions; `evaluate.py` exits non-zero on contract violations (output verified byte-identical before and after) |

Sample status accuracy moved from 17/32 (initial checkpoint) to 22, 25, 27, 29 and 31/32 across these fixes; sequence accuracy from 1/32 to 16/32. Findings and remediation precision and recall are 1.000.

## Output validation (518 evaluation cases)

Checked by `ghostaudit.validation.validate_output` (run by `code/evaluate.py` and `code/tests/test_output.py`) after the final run:

- 518 rows, header `case_id,deletion_status,steps_json`, unique IDs sorted by `case_id`.
- Every step has exactly the 7 required fields, a valid operation/state pair, consecutive numbering and FINDING → REMEDIATION → VERIFICATION order.
- Findings and COMPLETE verifications all cite evidence; remediation and INCOMPLETE verification steps use an empty `evidence_id`.
- No cited evidence is captured after the case's `audit_at`. All 1,082 citations name a file that contains the cited resource, or (33 citations) a covering SQL snapshot that proves its absence.
- Validator errors: 0.

| Status | Count |
| --- | --- |
| COMPLETE | 146 |
| INCOMPLETE | 363 |
| NOT_APPLICABLE | 9 |

INCOMPLETE breakdown: 354 with findings and 9 with no steps.

- **9 empty cases** (case_35, case_39, case_50, case_52, case_53, case_55, case_61, case_63, case_538). No admissible, machine-readable evidence links any record to the subject at `audit_at`: their evidence is limited to PNG screenshots linked by `LINK_RESOURCE_IDENTITY`, or the subject's records are captured only after `audit_at`. Windows OCR was tested on one screenshot; it flattens the table layout, so record state could not be read reliably and screenshots are not used. These rows conflict with "INCOMPLETE includes findings", but no finding is invented to satisfy that rule.
- **78 cases whose only evidence-linked record is a present, retained audit record** (e.g. case_103, case_107, case_108, case_111, case_126) are COMPLETE with that verification cited; 49 of them also gain workflow-linked account or analytics verifications. Every public sample with this pattern (case_21, case_25) is COMPLETE in the published answers.

## Known limitations

- PNG screenshots are not read (no OCR). PDFs are read only when `pdftotext` (poppler) is installed; without it, evidence_55 and evidence_58 are skipped and the output changes.
- Workflow events that remain ambiguous after all linkage constraints are not used.
- The request classifier is keyword-based. It is validated on all 32 samples, and the 9 evaluation cases it marks NOT_APPLICABLE were each reviewed by hand.

## How to reproduce

```bash
python code/main.py        # regenerate output.csv
python code/evaluate.py    # sample metrics above + contract validation
python -m unittest discover -s code -t code -v   # 43 tests, including the custom cases above
```
