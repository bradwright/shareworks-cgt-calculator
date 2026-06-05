# Agent Instructions

This repository is a local Shareworks PDF to CSV extractor. Do not add Google API credentials, Google client dependencies, or direct Google Sheets upload logic to the Python code.

## Local Extraction

Use the Python script to generate CSV output:

```bash
uv run python src/extract_shareworks_statement.py --in /path/to/shareworks-statement.pdf
```

Use `--out /path/to/output.csv` when the user wants a specific CSV path. If `--out` is omitted, the rows CSV is written to `outputs/shareworks_extracted_rows.csv`.

The script also writes an audit CSV to `outputs/shareworks_extraction_audit.csv`.

## Optional Google Sheets Load

Only perform a Google Sheets load when the user explicitly asks an agent to do it. Use the available Google Sheets/Drive connector or app tools in the agent environment rather than Python code in this repo.

For a target Google Sheets URL with a tab `gid`:

1. Resolve the spreadsheet ID and target sheet/tab from the URL.
2. Read the generated rows CSV.
3. Clear the target tab range `A:K`.
4. Paste/write the CSV rows starting at `A1`.
5. Freeze row `1`.
6. Apply monospace font formatting to columns `A` and `K`.
   - Use `Roboto Mono` when available.
7. Verify:
   - `A1:K1` matches the CSV header.
   - The loaded row count equals the CSV row count.
   - A final-row sentinel matches the CSV final row.

Do not require or set up Google Application Default Credentials for this repository. Google access belongs to the requesting user's agent/session.
