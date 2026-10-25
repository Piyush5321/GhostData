# Usage Report — Ghost Data Still Exists

## Final full-dataset run

The `output.csv` submitted with this solution was produced by:

```bash
python code/main.py
```

- Runtime: Python 3.12.11 (standard library only), with poppler `pdftotext` available on `PATH` for PDF evidence
- Requests processed: 518
- Output: COMPLETE 146, INCOMPLETE 363, NOT_APPLICABLE 9
- Wall-clock time: about 11 s for the full run
- Deterministic: repeated runs produce a byte-identical `output.csv` (MD5 `d7fc6841b54f36b3fd35db276ae12644`)

## Model usage

The agent is a deterministic rule engine. It does not call any language model or external API during the run.

| Provider | Model | Model calls | Input tokens | Output tokens | Total tokens |
| --- | --- | --- | --- | --- | --- |
| None | None | 0 | 0 | 0 | 0 |
| **Overall** | | **0** | **0** | **0** | **0** |

- Average tokens per request: 0
- Estimated total cost: $0.00
- Estimated cost per request: $0.00

## Development-time AI usage (not part of the run)

AI coding assistants were used to write and debug the code: Claude Code, Kiro, OpenCode and GitHub Copilot, as recorded in `log.txt`. Their token usage is not counted here, because they did not take part in producing `output.csv`. Their session traces are submitted separately in `agent-trace.zip`.

No API keys, credentials or configuration secrets are used by, or included in, the solution.
