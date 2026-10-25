# Ghost Data Still Exists — Deletion Audit Agent

Deterministic deletion-audit agent for the HackerRank Orchestrate (October 2026) challenge. It decides, for every deletion request, whether personal data is really gone at the audit time, and produces evidence-backed findings, an ordered remediation plan and verification checks.

| | |
| --- | --- |
| Evaluation requests | 518 (COMPLETE 146, INCOMPLETE 363, NOT_APPLICABLE 9), 0 contract violations |
| Public samples | status 31/32, exact step sequence 16/32, step precision 0.989 / recall 0.902 |

## Repository

```text
code/            the submission (code.zip): agent, tests, evaluation reports, README, ARCHITECTURE
output.csv       predictions for dataset/deletion_requests.csv
log.txt          AI-assistant development log (chat transcript)
AGENTS.md, problem_statement.md, agent_trace_instructions.md   challenge instructions
```

## Run

Place the challenge `dataset/` folder at the repository root (it is not redistributed), then:

```bash
python code/main.py                              # writes output.csv
python code/evaluate.py                          # sample metrics + contract check
python -m unittest discover -s code -t code -v   # 43 tests
```

Python 3.8+, standard library only; poppler's `pdftotext` is optional for PDF evidence.

Full documentation: [`code/README.md`](code/README.md), [`code/ARCHITECTURE.md`](code/ARCHITECTURE.md), [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).
