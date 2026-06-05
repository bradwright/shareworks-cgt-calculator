# Agent Instructions

This repository is a local Shareworks PDF to CSV extractor. Do not add Google API credentials, Google client dependencies, or direct Google Sheets upload logic to the Python code.

## Local Extraction

Use the Python script to generate CSV output:

```bash
uv run src/extract_shareworks_statement.py --in /path/to/shareworks-statement.pdf
```

Use `--out /path/to/output.csv` when the user wants a specific CSV path. If `--out` is omitted, the rows CSV is written to `outputs/shareworks_extracted_rows.csv`.

The script also writes an audit CSV to `outputs/shareworks_extraction_audit.csv`.

## Optional Google Sheets Load

Only perform a Google Sheets load when the user explicitly asks an agent to do it. Use the available Google Sheets/Drive connector or app tools in the agent environment rather than Python code in this repo.

For a target Google Sheets URL with a tab `gid`:

1. Resolve the spreadsheet ID and target sheet/tab from the URL.
2. Read the generated rows CSV.
3. Read the existing target tab range `A:K`.
4. Ensure row `1` contains the CSV header. If the sheet is empty, write the header to `A1:K1`.
5. Build a uniqueness key from columns `A:C`: `ID`, `Date`, and `Type`.
6. For each CSV data row:
   - If the `A:C` key is not present in the sheet, append the row to the first empty row.
   - If the `A:C` key is already present, leave the existing row unchanged.
   - If duplicate `A:C` keys already exist in the sheet, stop and report the duplicate keys instead of writing.
7. Do not clear or overwrite the full target range unless the user explicitly asks for a full replacement load.
8. Freeze row `1`.
9. Apply monospace font formatting to columns `A` and `K`.
   - Use `Roboto Mono` when available.
10. Verify:
   - `A1:K1` matches the CSV header.
   - Every CSV data row has exactly one matching sheet row by the `A:C` key.
   - The sheet has no duplicate `A:C` keys.
   - The number of appended rows equals the number of CSV keys that were not already present before the load.

Do not require or set up Google Application Default Credentials for this repository. Google access belongs to the requesting user's agent/session.
