# CGT Calculator Data Extractor

Extract Shareworks RSU activity from a downloaded statement PDF into CSV rows that match the Google Sheet `activity!A:K` shape used for UK CGT calculations.

The Python code is local-only: it parses a PDF and writes CSV files. It does not authenticate to Google or upload to Google Sheets.

## Usage

Run from the project root:

```bash
uv run python src/extract_shareworks_statement.py --in ~/Downloads/statement.pdf
```

By default this writes:

- `outputs/shareworks_extracted_rows.csv`
- `outputs/shareworks_extraction_audit.csv`

Use `--out` to choose the rows CSV path:

```bash
uv run python src/extract_shareworks_statement.py \
  --in ~/Downloads/statement.pdf \
  --out my-rsu-activity.csv
```

Scratch files and the Frankfurter USD-to-GBP FX cache are written to `work/`.

The cache file is intentionally ignored by git. If it is absent, the script fetches historical rates from Frankfurter. Network access is required for uncached rates.

## Google Sheets

To use the output in Google Sheets, paste `shareworks_extracted_rows.csv` into the target tab starting at `A1`.

If you want an agent to load the CSV for you, point it at this repository's [AGENTS.md](AGENTS.md). The agent instructions describe the upload and formatting workflow:

- clear target `A:K`
- paste the CSV at `A1`
- freeze row `1`
- format columns `A` and `K` as monospace

## Output Shape

The extractor writes rows matching `activity!A:K`:

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

## CGT Calculator String

Column K uses this shape:

```text
B|S dd/mm/yyyy SHOP quantity price fees 0.00
```

The Python implementation outputs:

- `B` for `Release`, otherwise `S`
- date as `dd/mm/yyyy`
- quantity fixed to 6 decimals
- GBP price fixed to 4 decimals
- GBP fees fixed to 2 decimals
- trailing `0.00`

## Validation Notes

The development statement produced:

- 44 release sections
- 44 `Release` rows
- 44 `Sell to Cover` rows
- 30 `Withdrawal` rows
- 118 spreadsheet rows
- 100 unique FX lookup dates
- 0 failed parsed sections

## Caveats

- Frankfurter is not XE. The original instructions mention XE, but this script currently uses Frankfurter because it has a simple historical API. Values are expected to be close but not identical.
- Frankfurter may return the nearest available published rate for a non-rate day. The audit CSV records the lookup date and returned rate date.
- The script is tuned to the text layout extracted from the current Shareworks PDF format. If Shareworks changes statement wording or layout, parser patterns may need adjustment.

## Dependencies

The script uses:

- `pypdf`

Install dependencies with:

```bash
uv sync
```

For a plain requirements-based install:

```bash
uv pip install -r requirements.txt
```
