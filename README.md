# Ghost Data Still Exists - Deletion Audit Agent

A deterministic deletion-audit agent for the HackerRank Orchestrate October 2026 challenge. For each deletion request it resolves the person's current and historical identifiers and reconstructs the state of every linked record at the case's `audit_at`, using the supplied evidence. It then decides whether the deletion is `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`. It outputs the findings, the ordered remediation plan and the verification checks.

The agent is rule-based Python: it makes no language-model or API calls at run time. AI coding assistants were used to build it (see `log.txt`).

## Setup

- Python 3.8+ (developed and run on 3.12), standard library only
- Optional: poppler's `pdftotext` on `PATH` to read PDF evidence. Without it, PDFs are skipped and two evidence files (evidence_55, evidence_58) are not used.
- Place the challenge `dataset/` folder at the repository root (it is not included in this repository).

## Run

From the repository root:

```bash
python code/main.py
```

This reads `dataset/` and writes `output.csv` (518 rows) to the repository root in about 7 seconds.

## Tests

```bash
python code/test_evidence_state.py   # evidence parsing, state and time-filter regressions
python code/test_log_matching.py     # identifier matching regressions
python code/test_audit.py            # core helpers
python code/test_fixes.py            # classifier and presence edge cases (prints OK/FAIL per case)
```

## How it works

All logic is in `code/main.py`. `ARCHITECTURE.md` describes it in detail.

1. **Request classification** (`is_deletion_request`): multilingual keyword stems with verb-specific negations. Non-deletion requests become `NOT_APPLICABLE` with `[]`.
2. **Identity resolution** (`build_identity_index`, `resolve_subject`): current email, phone and email hash, plus historical values from `user_audit_log.csv` (`old_value`/`new_value`).
3. **Evidence parsing** (`load_all_evidence`): SQL snapshots (in-memory SQLite, including JSON payload columns and `snapshot_scope`); JSON exports; six log layouts grouped by trace and split per resource; the handover text table; and PDFs. Each record gets a canonical `system_id` from its resource prefix and a store (primary or a named replica).
4. **Matching** (`find_relevant_records`): whole-token identifier matching and explicit `LINK_RESOURCE_IDENTITY` catalog links. Evidence captured after `audit_at`, and records whose own `recorded_at`/`observed_at` is later, are excluded.
5. **State** (`latest_observations`): the latest observation per store. A copy still held by any store counts as remaining. Omission from a later SQL snapshot that covers the store proves absence, but is never used to declare a retained audit record missing.
6. **Policy and steps** (`audit_case`, `build_steps`): `deletion_rules.csv` actions produce the finding, remediation and verification patterns, ordered by phase, operation, `system_id` and `resource_id`.

A case is `COMPLETE` only when no finding exists and at least one copy outside the retained security audit log is verified. Otherwise it is `INCOMPLETE`. Findings and COMPLETE verifications cite evidence; proposed remediation and future checks use an empty `evidence_id`.

## Results

Status counts for the 518 cases: COMPLETE 68, INCOMPLETE 441, NOT_APPLICABLE 9. On the 32 public samples: status 27/32, findings precision and recall 1.000, exact step sequences 8/32. Full metrics, custom evaluation cases and the change history are in `code/evaluation/evaluation_report.md`; run statistics are in `code/evaluation/usage_report.md`.

## Known limitations

- PNG screenshots are not read (no OCR).
- Records linked to a person only by the timing of `system_activity` events are not used, because that linkage is ambiguous in the data.
- 9 INCOMPLETE cases have no steps and 78 contain only a retained audit-record verification, because no other admissible, machine-readable evidence exists for them. No finding is invented to fill these.

## Repository layout

| Path | Purpose |
| --- | --- |
| `code/main.py` | the agent (entry point) |
| `code/test_*.py` | regression and unit tests |
| `code/evaluation/` | `evaluation_report.md`, `usage_report.md` (packaged as `evaluation/` at the root of `code.zip`) |
| `output.csv` | submitted predictions |
| `log.txt` | AI-assistant development log (append-only) |
| `ARCHITECTURE.md` | design and data flow |
| `AGENTS.md`, `problem_statement.md` | challenge instructions |

Challenge page: https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
