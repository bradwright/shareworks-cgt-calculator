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
uv run src/extract_shareworks_statement.py --in ~/Downloads/statement.pdf
```

Saved HTML reports are also supported:

```bash
uv run src/extract_shareworks_statement.py --in ~/Downloads/account-summary.htm
```

By default, this writes:

- `outputs/shareworks_extracted_rows.csv`
- `outputs/shareworks_extraction_audit.csv`

Use `--out` to choose the rows CSV path:

```bash
uv run src/extract_shareworks_statement.py \
  --in ~/Downloads/statement.pdf \
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

The audit CSV includes parser status, source dates, FX lookup details, parsed quantities, fees, and notes for each Shareworks statement section.

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
- `CGT Calculator String` is generated directly from the parsed transaction fields.

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

## Using CGTCalculator

This project prepares the trade lines for [CGTCalculator](https://www.cgtcalculator.com/calculator.aspx); it does not calculate your final UK CGT liability itself.

After generating or loading the CSV:

1. Copy every value from column `K` (`CGT Calculator String`), excluding the header.
2. Open [CGTCalculator](https://www.cgtcalculator.com/calculator.aspx).
3. Paste the copied lines into the trades text area.
4. Optionally click `Format/Sort`.
5. Make sure `Apply rounding` is unchecked.
6. Click `CALCULATE`.

Paste the full history of generated trade lines, not only the rows for a single tax year. CGTCalculator needs the surrounding transactions to apply same-day matching, Section 104 pooling, and the 30-day rule correctly. This is especially important for Shareworks RSUs because `Release` and `Sell to Cover` rows often match on the same day, and sell-to-cover fees can create small losses that would be distorted by rounding.

## Caveats

- FX rates are sourced from Frankfurter, not XE or HMRC. Values from different providers may be close but not identical.
- Frankfurter may return the nearest available published rate for a non-rate day. The audit CSV records the lookup date and returned rate date.
- The PDF parser is tuned to the text layout extracted from the current Shareworks PDF format. If Shareworks changes statement wording or layout, parser patterns may need adjustment.
- The HTML parser expects the classic saved Account Summary report tables, not the modern Morgan Stanley at Work web-app bootstrap page.

## Dependencies

The script declares its dependency inline using PEP 723 script metadata:

- `pypdf`

`uv run src/extract_shareworks_statement.py ...` reads that metadata and creates the required environment automatically.

## Appendix: Google Sheets

The extractor does not contain Google API credentials, Google client dependencies, or upload logic. If you want the output in Google Sheets, use one of these workflows:

- Import or paste `shareworks_extracted_rows.csv` into a sheet starting at `A1`.
- Ask an agent with Google Sheets access to load the generated CSV for you.

When using an agent-assisted load, a good workflow is to append only missing rows, using `ID`, `Date`, and `Type` as the uniqueness key. Existing rows should be left unchanged unless you explicitly want a full replacement load.
