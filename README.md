# Shareworks CGT CSV Extractor

A local command-line extractor for converting Shareworks RSU statement PDFs or saved Account Summary HTML pages into CSV rows suitable for UK capital gains tax workflows and [CGTCalculator](https://www.cgtcalculator.com/calculator.aspx).

The extractor parses a downloaded Shareworks statement PDF or a saved classic Account Summary HTML page, writes a transaction CSV, and produces an audit CSV showing how each source section was interpreted. It runs locally and does not authenticate to Google or upload data anywhere.

## Requirements

- Python 3.10 or later
- [uv](https://docs.astral.sh/uv/)

## Getting the Shareworks Report

Download a Shareworks statement PDF or save the classic Account Summary HTML page before running the extractor:

1. Sign in to Shareworks or Morgan Stanley at Work.
2. Open `Activity` -> `Reports` -> `Account Summary`.
3. Set `Period Quick Select` to `All Available History`.
4. Under `Product Selection`, select both `Share Purchase and Holdings` and `Stock Options and Awards`. These are often selected by default.
5. Under `Account Summary Type`, select `Full`. This is often selected by default.

For PDF extraction:

1. Under `View As`, select `PDF` and `A4`.
2. Submit the report and save the generated PDF locally.

For HTML extraction:

1. Under `View As`, select `Web Page`.
2. Submit the report.
3. Save the rendered report page from the browser.
4. In the browser save dialog, set `Format` to `Webpage, Complete`.
5. Save the `.htm` file and its accompanying `_files` folder together, usually into `Downloads`.

For HTML extraction, the saved file should be named like `Morgan Stanley at Work - Account Summary.htm` and should contain the rendered classic `userStatement.do` report tables. A saved single-page app shell with only an `app-mount` element is not enough.

Shareworks labels and navigation vary between employers and account migrations. The important part is to download or save the detailed statement that includes release, sell-to-cover, withdrawal, settlement, fee, and price details.

## Usage

Run from the project root:

```bash
uv run src/extract_shareworks_statement.py \
  --in ~/Downloads/statement.pdf \
  --cgt-symbol SYMBOL
```

Saved HTML reports are also supported:

```bash
uv run src/extract_shareworks_statement.py \
  --in ~/Downloads/account-summary.htm \
  --cgt-symbol SYMBOL
```

By default, this writes:

- `outputs/shareworks_extracted_rows.csv`
- `outputs/shareworks_extraction_audit.csv`

The command prints `cgt_ready=true` only when the extracted transactions reconcile to the statement's independent Activity ledgers, FX enrichment succeeds, and no blocking completeness issue is found. A blocked run exits non-zero, still writes the audit and raw transaction rows, and leaves every FX, GBP, and `CGT Calculator String` value blank. Existing rows output is neutralized before parsing or network work so a failed rerun cannot leave stale CGT lines in place.

Use `--out` to choose the rows CSV path:

```bash
uv run src/extract_shareworks_statement.py \
  --in ~/Downloads/statement.pdf \
  --cgt-symbol SYMBOL \
  --out my-rsu-activity.csv
```

Scratch files and the Frankfurter USD-to-GBP FX cache are written to `work/`.

If the cache is absent, the script fetches historical rates from Frankfurter. Network access is required for uncached rates.

## Output Shape

The extractor writes the following columns:

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

The audit CSV includes parser status, source dates, FX lookup details, parsed quantities, fees, and notes for each Shareworks statement section. It also records opening shares, reported and calculated closing shares, the minimum running balance, and any reconciliation difference.

## Rules Implemented

- Release rows use the Shareworks `Release Date`.
- Sell to Cover rows use the Shareworks `Release Date` as both the disposal date and FX lookup date; the later settlement date is retained only for audit and holdings-ledger reconciliation.
- Withdrawal rows use the `Withdrawal on ...` heading date, not settlement date.
- Brokerage commission and supplemental transaction fees are included in USD fees.
- Wire/EFT transfer fees are excluded.
- FX rates are fetched as GBP per 1 USD from Frankfurter historical rates.
- `GBP Price = USD Price * FX Rate`.
- `GBP Fees = USD Fees * FX Rate`; blank USD fees are treated as zero.
- `Running total` starts with the statement's Activity-table opening holding, increments on `Release` rows, and decrements on `Sell to Cover` and `Withdrawal` rows.
- The calculated closing holding must match the Activity-table closing holding within `0.000001` shares.
- `--cgt-symbol` is required, must be an uppercase one-word identifier, and is emitted only when the statement contains exactly one unique fund.
- Gross release values and withdrawal gross proceeds must match quantity multiplied by USD unit price within `$0.01`.
- Holdings Activity releases and sales are independently matched to the detailed release and withdrawal sections; the RSU Activity ledger is also matched so zero-net releases cannot disappear silently.
- Missing, duplicate, or malformed holding snapshots; negative running holdings; and closing-balance mismatches block CGT-ready output.
- Unknown Activity rows, missing or extra detail sections, duplicate release IDs, date/quantity mismatches, and FX failures also block CGT-ready output.
- A non-zero opening holding also blocks CGT-ready output because its original acquisition dates and historic GBP costs are not present in the extracted transactions.
- `CGT Calculator String` is generated directly from the parsed transaction fields.

## CGT Calculator String

Column K uses this shape:

```text
B|S dd/mm/yyyy SYMBOL quantity price fees 0.00
```

The Python implementation outputs:

- `B` for `Release`, otherwise `S`
- date as `dd/mm/yyyy`
- the security identifier supplied with `--cgt-symbol`
- quantity fixed to 6 decimals
- GBP price fixed to 4 decimals
- GBP fees fixed to 2 decimals
- trailing `0.00`

## Using CGTCalculator

This project prepares the trade lines for [CGTCalculator](https://www.cgtcalculator.com/calculator.aspx); it does not calculate your final UK CGT liability itself.

Only continue when the command reports `cgt_ready=true`. If it reports `cgt_ready=false`, inspect the audit CSV and supply the missing acquisition history or correct the source statement before using the trade lines.

After generating a CGT-ready CSV:

1. Copy every value from column `K` (`CGT Calculator String`), excluding the header.
2. Open [CGTCalculator](https://www.cgtcalculator.com/calculator.aspx).
3. Paste the copied lines into the trades text area.
4. Optionally click `Format/Sort`.
5. Make sure `Apply rounding` is unchecked.
6. Click `CALCULATE`.

Paste the full history of generated trade lines, not only the rows for a single tax year. CGTCalculator needs the surrounding transactions to apply same-day matching, Section 104 pooling, and the 30-day rule correctly. This is especially important for Shareworks RSUs because `Release` and `Sell to Cover` rows often match on the same day, and sell-to-cover fees can create small losses that would be distorted by rounding.

An Activity-table opening market price or book value is not enough to reconstruct a UK CGT acquisition. The extractor uses opening shares only to reconcile the running balance and never invents a synthetic purchase row.

## Caveats

- FX rates are sourced from Frankfurter, not XE or HMRC. Values from different providers may be close but not identical.
- Frankfurter may return the nearest available published rate for a non-rate day. The audit CSV records the lookup date and returned rate date.
- Sell-to-cover handling treats Shareworks `Release Date` as the trade/disposal date because the supported statement supplies no separate trade date. If a transaction confirmation identifies a different contract/trade date, reconcile that source before using the output.
- The PDF parser is tuned to the text layout extracted from the current Shareworks PDF format. If Shareworks changes statement wording or layout, parser patterns may need adjustment.
- The HTML parser expects the classic saved Account Summary report tables, not the modern Morgan Stanley at Work web-app bootstrap page.

## Tests

Run the parser and fail-closed regression suite from the project root:

```bash
uv run --with pypdf python -m unittest discover -s tests -v
```

## Dependencies

The script declares its dependency inline using PEP 723 script metadata:

- `pypdf`

`uv run src/extract_shareworks_statement.py ...` reads that metadata and creates the required environment automatically.

## Appendix: Google Sheets

The extractor does not contain Google API credentials, Google client dependencies, or upload logic. If you want the output in Google Sheets, use one of these workflows:

- Import or paste `shareworks_extracted_rows.csv` into a sheet starting at `A1`.
- Ask an agent with Google Sheets access to load the generated CSV for you.

When using an agent-assisted load, a good workflow is to append only missing rows, using `ID`, `Date`, and `Type` as the uniqueness key. Existing rows should be left unchanged unless you explicitly want a full replacement load.
