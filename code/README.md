# Ghost Data Still Exists — Deletion Audit Agent

A deterministic agent that audits personal-data deletion requests for the HackerRank Orchestrate (October 2026) challenge. For every request it:

1. decides whether the request actually authorizes deletion;
2. resolves the person's current and historical identifiers;
3. reconstructs, at the case's `audit_at`, the state of every record linked to that person across databases, replicas, logs, exports, backups and documents;
4. applies `deletion_rules.csv` and reports `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with evidence-backed findings, an ordered remediation plan and verification checks.

The agent is rule-based Python and makes **no language-model or network calls** at run time, so results are reproducible and cost nothing to run. AI coding assistants were used during development; see `log.txt`.

## Results

| | |
| --- | --- |
| Evaluation requests | 518 (COMPLETE 146, INCOMPLETE 363, NOT_APPLICABLE 9) |
| Output contract violations | 0 |
| Public samples — status accuracy | 31 / 32 (0.969) |
| Public samples — findings and remediation | precision 1.000, recall 1.000 |
| Public samples — all steps | precision 0.989, recall 0.902 |
| Public samples — exact step sequence | 16 / 32 |
| Full run time | ~11 s |

Details: [`evaluation/evaluation_report.md`](evaluation/evaluation_report.md) and [`evaluation/usage_report.md`](evaluation/usage_report.md) (paths relative to `code/`).

## Quick start

Requirements: Python 3.8+ (standard library only). Optional: poppler's `pdftotext` on `PATH` to read PDF evidence. Without it, PDFs are skipped and two evidence files are not used.

This directory is the submitted `code.zip`. It expects to sit as `code/` in the challenge repository, next to `dataset/`; the output is written to the repository root. From the repository root:

```bash
python code/main.py                              # writes output.csv (518 rows)
python code/evaluate.py                          # sample metrics + output contract check
python -m unittest discover -s code -t code -v   # 43 unit and regression tests
```

From inside `code/`, `python -m unittest -v` runs the same tests. `main.py` and `evaluate.py` accept `--dataset DIR` and `--output FILE` for other locations.

## Output

`output.csv` has the columns `case_id,deletion_status,steps_json`, sorted by `case_id`. Each step is:

```json
{"step_no": 1, "phase": "FINDING", "system_id": "account_service", "resource_id": "account_005",
 "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_54"}
```

- **COMPLETE** — verification steps only, each citing the evidence that proves the state.
- **INCOMPLETE** — findings citing evidence, then remediation and verification steps with an empty `evidence_id` (future work).
- **NOT_APPLICABLE** — `[]`.

Steps are ordered by phase, then by the operation order in the problem statement, then by `system_id` and `resource_id`.

## How it works

```text
deletion_requests.csv ─► classifier ─► identity ─► evidence selection ─► state at audit_at ─► policy ─► steps
                                                     ▲
              evidence_files.csv + dataset/evidence/ ┘ (SQL, JSON, 6 log layouts, handover text, PDF)
```

- **Request classification**: multilingual deletion stems with verb-specific negations.
- **Identity**: current email, phone and email hash, plus historical values from `user_audit_log.csv`. Matching is whole-token, so `user_11` never matches `user_110`.
- **Workflow linkage**: deletion-worker events name resources but not people. An event is linked to a request only when timing, a result consistent with the reported status, evidence ownership and uniqueness leave a single candidate (73 correct, 0 wrong links on the samples). Event results stay claims; state comes from evidence.
- **Evidence**: every format is normalized into records with a canonical `system_id`, a store (primary or a named replica) and the manifest capture time. Log lines are grouped by trace.
- **State at `audit_at`**: only evidence captured, and observed, at or before `audit_at` is used. Each store's latest observation decides. Any store still holding a copy means the record remains. Absence from a later SQL snapshot that covers the store proves deletion.
- **Policy**: data flows, restore risks, retained audit records, tombstones, and delete / redact / anonymize rules.
- **Status**: COMPLETE when admissible evidence verifies the subject's records in their required state and nothing remains, including when the only linked record is a retained audit record; INCOMPLETE when a violation is found or no evidence links any record to the subject.

[`ARCHITECTURE.md`](ARCHITECTURE.md) documents each module and decision rule.

## Layout of code/

```text
main.py          entry point
evaluate.py      sample scoring and contract validation
ghostaudit/      the agent: classifier, identity, evidence, linkage, state, policy, audit, validation
tests/           unittest suite (43 tests)
evaluation/      evaluation_report.md, usage_report.md
README.md        this file
ARCHITECTURE.md  design and data flow
```

The repository root holds `output.csv` (predictions for the 518 evaluation requests), `log.txt` (the AI-assistant development log) and the challenge instructions. `dataset/` is the challenge input and is not redistributed.

## Known limitations

- PNG screenshots are not read: no OCR engine is bundled, and Windows OCR flattens their table layouts.
- Workflow events that stay ambiguous after all linkage constraints (e.g. simultaneous analytics anonymizations for equivalent requests) are not linked.
- 9 INCOMPLETE cases have no steps, because no admissible, machine-readable evidence links any record to the subject at `audit_at`. No finding is invented to fill these.
