# CGT Calculator Data Extractor

Extract Shareworks RSU activity from a statement PDF into spreadsheet rows that match the `activity` tab shape used for UK CGT calculations.

The extractor currently produces:

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
python3 src/extract_shareworks_statement.py --pdf ~/Downloads/statement.pdf
```

Outputs are written to `outputs/`:

- `shareworks_extracted_rows.csv`
- `shareworks_extracted_rows.xlsx`
- `shareworks_extraction_audit.csv`

Scratch files and the Frankfurter USD-to-GBP FX cache are written to `work/`.

## Rules Implemented

- Release rows use the Shareworks `Release Date`.
- Sell to Cover rows use the release `Settlement Date` for FX lookup.
- Withdrawal rows use the `Withdrawal on ...` heading date, not settlement date.
- Brokerage commission and supplemental transaction fees are included in USD fees.
- Wire/EFT transfer fees are excluded.
- FX rates are fetched as GBP per 1 USD from Frankfurter historical rates.
- `CGT Calculator String` mirrors the Google Sheets formula from the `activity` tab.

## Dependencies

The script uses:

- `pandas`
- `openpyxl`
- `pypdf`

Install them in your preferred Python environment if they are not already available.

