# Evaluation Report - Ghost Data Still Exists

The agent is a deterministic, rule-based Python program (`code/main.py`). It makes no model or API calls. Every number below comes from running the current code against the participant-visible files only.

## How accuracy was measured

The only labelled data is `dataset/sample_deletion_requests.csv` (32 cases) with `dataset/sample_output.csv`. Each sample case is run through `audit_case()` in memory and compared with the published answer.

- **Status accuracy**: share of cases whose `deletion_status` matches.
- **Step-level accuracy**: precision and recall of steps, where a step matches when `phase`, `system_id`, `resource_id`, `operation`, `state` and `evidence_id` are all equal.
- **Field-level accuracy**: steps aligned by position; each of the 7 fields is compared; denominator = 7 × max(len(gold), len(predicted)) per case.
- **Sequence accuracy**: share of cases whose ordered step list matches exactly.

There are no labels for the 518 evaluation cases, so their correctness is checked structurally and by inspecting the cited evidence (see "Output validation").

## Results on the 32 sample cases

| Metric | Value |
| --- | --- |
| Status accuracy | 27 / 32 (84.4%) |
| Sequence accuracy (exact ordered steps) | 8 / 32 (25.0%) |
| Step-level precision / recall | 0.981 / 0.808 |
| Field-level accuracy (position-aligned) | 0.700 |

Status confusion (gold → predicted): COMPLETE → COMPLETE 5, COMPLETE → INCOMPLETE 5, INCOMPLETE → INCOMPLETE 22. No INCOMPLETE case was predicted COMPLETE, and no case was misclassified as NOT_APPLICABLE.

| Phase | True positives | False positives | False negatives | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| FINDING | 41 | 0 | 0 | 1.000 | 1.000 |
| REMEDIATION | 41 | 0 | 0 | 1.000 | 1.000 |
| VERIFICATION | 74 | 3 | 37 | 0.961 | 0.667 |

Per-field agreement (194 aligned positions): step_no 0.814, phase 0.814, system_id 0.588, resource_id 0.588, operation 0.644, state 0.644, evidence_id 0.804.

### Why the remaining samples differ

All remaining differences are in VERIFICATION. They concern resources that no admissible evidence links to the subject:

- The 5 COMPLETE samples predicted INCOMPLETE (case_01, case_02, case_04, case_21, case_25) depend on records such as `account_001` or `account_3136`. In the evidence these appear only as a missing snapshot row or a 404 with a null profile, with no subject identifier. The only connection to the subject is the timing of `system_activity` events. Measured on the data, each authorized case has 4-7 candidate account-deletion events in its window, so this linkage is ambiguous and was deliberately not implemented.
- Several gold INCOMPLETE verifications cite resources that appear in no evidence file at all (for example `audit_record_5569` for case_20).
- The 3 extra verification steps are resources genuinely linked to the subject by evidence but not listed in the gold answer (for example `account_6523` for case_17, an earlier lookup by the subject's own email).

## Additional evaluation cases (created by the team)

These are regression tests in `code/test_evidence_state.py`, `code/test_log_matching.py` and `code/test_fixes.py`. Each one reproduces a failure found in the real data. All pass.

| # | Case | Input | Expected | Result |
| --- | --- | --- | --- | --- |
| 1 | NOT_FOUND lookup (evidence_13, case_133) | JSON record `result: "NOT_FOUND"`, `record_type: USER_PROFILE` | account absent → VERIFY_RECORD_ABSENT, no finding | pass |
| 2 | Cache miss inside a trace | trace with `cache_lookup result=miss` followed by `rows=1` PII scan | record present (a cache miss is not absence) | pass |
| 3 | Replica drift (case_05 shape) | replica row present on 09-02, primary readback 404 on 09-05 | REPORT_REMAINING_RECORD from the replica snapshot | pass |
| 4 | Historical email (case_12 shape) | request from an address found only in `user_audit_log.old_value` | resolves to the stable user and both emails | pass |
| 5 | Record dated after audit_at (case_135) | admissible file containing a record with `recorded_at` after `audit_at` | that record is ignored | pass |
| 6 | Scoped snapshot (evidence_90) | SQL file with `snapshot_scope = account_service` and empty tables for other systems | coverage only for `account_service` | pass |
| 7 | Prefix collision (case_34) | subject `user_11`, log line about `user_110` | no match | pass |
| 8 | Four log layouts | `requestId/message=`, JSON-lines, pipe, RFC 5424 syslog | lookup line joined to its resource line by trace | pass |
| 9 | Multilingual / negated requests | Spanish, Portuguese, French, Japanese, Korean requests; "not a request to erase it" | deletion vs NOT_APPLICABLE | pass |

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

Sample status accuracy moved from 17/32 (initial checkpoint) to 22, 25, 27/32 across these fixes. Findings precision and recall are 1.000.

## Output validation (518 evaluation cases)

Checked with a contract validator after the final run:

- 518 rows, header `case_id,deletion_status,steps_json`, unique IDs sorted by `case_id`.
- Every step has exactly the 7 required fields, a valid operation/state pair, consecutive numbering and FINDING → REMEDIATION → VERIFICATION order.
- Findings and COMPLETE verifications all cite evidence; remediation and INCOMPLETE verification steps use an empty `evidence_id`.
- No cited evidence is captured after the case's `audit_at`; all 916 citations name a file that contains the cited resource (2 by omission from a covering SQL snapshot).
- Validator errors: 0.

| Status | Count |
| --- | --- |
| COMPLETE | 68 |
| INCOMPLETE | 441 |
| NOT_APPLICABLE | 9 |

INCOMPLETE breakdown: 353 with findings; 78 with only a retained security-audit verification; 10 with no steps. For the 10 empty cases, the subject's evidence is captured only after `audit_at` (9 cases) or does not exist (case_63). The 78 verification-only cases contain no admissible evidence beyond the retained audit record. These conflict with "INCOMPLETE includes findings". No finding is invented to satisfy that rule.

## Known limitations

- PNG screenshots are not read (no OCR). PDFs are read only when `pdftotext` (poppler) is installed; without it, evidence_55 and evidence_58 are skipped and the output changes.
- Resources linked to a subject only by `system_activity` timing are not used.
- The request classifier is keyword-based. It is validated on all 32 samples and the 9 eval cases it marks NOT_APPLICABLE were each reviewed by hand.

## How to reproduce

```bash
python code/main.py
python code/test_evidence_state.py
python code/test_log_matching.py
python code/test_audit.py
python code/test_fixes.py
```
