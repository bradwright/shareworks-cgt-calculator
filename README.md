# CGT Calculator Data Extractor

Extract Shareworks RSU activity from a statement PDF into spreadsheet rows that match the Google Sheet `activity` tab used for UK CGT calculations.

This project was created from a Codex working session so the extraction logic can be version controlled and iterated safely.

## Source Documents

- Instructions doc: <https://docs.google.com/document/d/1Y7aF80MGaLZW8qjVi8bAjZ6RW5UWLRhMkOJ7BYFGDiY/edit?tab=t.0#heading=h.szqafsxp1zr1>
- Working Google Sheet: <https://docs.google.com/spreadsheets/d/1MlPi94AqW5Ua5zPn5jPh_q_BiyeZnGc20SKyvWy9Skg/edit>
- `activity` tab: <https://docs.google.com/spreadsheets/d/1MlPi94AqW5Ua5zPn5jPh_q_BiyeZnGc20SKyvWy9Skg/edit?gid=684371624#gid=684371624>
- `Codex` comparison tab: <https://docs.google.com/spreadsheets/d/1MlPi94AqW5Ua5zPn5jPh_q_BiyeZnGc20SKyvWy9Skg/edit?gid=896751152#gid=896751152>
- Test PDF used during development: `/Users/bradwright/Downloads/statement.pdf`

## Current Output Shape

The extractor currently writes rows matching `activity!A:K`:

- `ID`
- `Date`
- `Type`
- `Quantity`
- `Running total`
- `USD Price`
- `USD Fees`
- `FX Rate`
- `GBP Price`
- `GBP Fees`
- `CGT Calculator String`

## Workflow

Run from the project root:

```bash
uv run python src/extract_shareworks_statement.py --pdf ~/Downloads/statement.pdf
```

Outputs are written to `outputs/`:

- `shareworks_extracted_rows.csv`
- `shareworks_extracted_rows.xlsx`
- `shareworks_extraction_audit.csv`

Scratch files and the Frankfurter USD-to-GBP FX cache are written to `work/`.

The cache file is intentionally ignored by git. If it is absent, the script fetches historical rates from Frankfurter. Network access is required for uncached rates.

## Rules Implemented

- Release rows use the Shareworks `Release Date`.
- Sell to Cover rows use the release `Settlement Date` for FX lookup.
- Withdrawal rows use the `Withdrawal on ...` heading date, not settlement date.
- Brokerage commission and supplemental transaction fees are included in USD fees.
- Wire/EFT transfer fees are excluded.
- FX rates are fetched as GBP per 1 USD from Frankfurter historical rates.
- `GBP Price = USD Price * FX Rate`.
- `GBP Fees = USD Fees * FX Rate`; blank USD fees are treated as zero.
- `Running total` increments on `Release` rows and decrements on `Sell to Cover` and `Withdrawal` rows.
- `CGT Calculator String` mirrors the Google Sheets formula from the `activity` tab.

## Google Sheet Formula Copied

Column K in `activity` uses this row formula:

```gs
=IF(C2="Release","B","S") & " " & TEXT(B2,"dd/mm/yyyy") & " SHOP " & FIXED(D2, 6, TRUE) & " " & FIXED(I2, 4, TRUE) & " " & FIXED(J2, 2, TRUE) & " 0.00"
```

The Python implementation outputs the same shape:

- `B` for `Release`, otherwise `S`
- Date as `dd/mm/yyyy`
- Quantity fixed to 6 decimals
- GBP price fixed to 4 decimals
- GBP fees fixed to 2 decimals
- trailing `0.00`

## Work Done So Far

- Read and summarized the Google Doc instructions.
- Parsed the Shareworks PDF with `pypdf`.
- Extracted Shareworks `Share Units - Release (...)` sections.
- Extracted Shareworks `Withdrawal on ...` sections.
- Generated rows for `Release`, `Sell to Cover`, and `Withdrawal`.
- Added allowed fee handling:
  - include brokerage commission
  - include supplemental transaction fee
  - exclude wire/EFT transfer fees
- Fixed withdrawal date handling to use the `Withdrawal on ...` heading date, per the instructions.
- Added historical USD-to-GBP FX lookup using Frankfurter.
- Cached FX rates locally in `work/frankfurter_usd_gbp_rates.json`.
- Added derived columns through `activity!K`:
  - running total
  - GBP price
  - GBP fees
  - CGT calculator string
- Pasted generated rows into the Google Sheet `Codex` tab for visual comparison.
- Initialized this git project and committed the first working extractor.

## Validation Notes

The development statement produced:

- 44 release sections
- 44 `Release` rows
- 44 `Sell to Cover` rows
- 30 `Withdrawal` rows
- 118 spreadsheet rows
- 100 unique FX lookup dates
- 0 failed parsed sections

Earlier comparisons against the existing `activity` tab showed:

- IDs matched for the recent rows present in `activity`.
- FX rates were close to manually-entered sheet rates, with expected differences from rate source and date policy.
- Column I/J math behaved like the sheet formulas, with observed differences explained by FX and fee-source differences.
- Column K was copied from the exact `activity` formula pattern.

## Current Caveats

- Frankfurter is not XE. The instructions mention XE, but this script currently uses Frankfurter because it has a simple historical API. Values are expected to be close but not identical.
- Frankfurter may return the nearest available published rate for a non-rate day. The audit CSV records the lookup date and returned rate date.
- The script is tuned to the text layout extracted from the current Shareworks PDF format. If Shareworks changes statement wording or layout, parser patterns may need adjustment.
- The Google Sheet paste step is not automated in this repository yet; it has been done through the Codex Google Drive connector during development.

## Dependencies

The script uses:

- `pandas`
- `openpyxl`
- `pypdf`

Install them in your preferred Python environment if they are not already available:

```bash
uv sync
```

For a plain requirements-based install, `requirements.txt` is also compatible with `uv`:

```bash
uv pip install -r requirements.txt
```

## Suggested Next Steps

- Add an automated Google Sheets upload/update command if this should become more than a local extractor.
- Add unit tests with representative snippets from release and withdrawal sections.
- Add a fixture-based test for the CGT calculator string formatting.
- Decide whether Frankfurter is acceptable, or whether exact XE parity is required.
