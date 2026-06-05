import argparse
import subprocess
import sys
from pathlib import Path


PDF_PATH = Path("/Users/bradwright/Downloads/statement.pdf")
OUT_DIR = Path("outputs")
WORK_DIR = Path("work")
CSV_PATH = OUT_DIR / "shareworks_extracted_rows.csv"
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1MlPi94AqW5Ua5zPn5jPh_q_BiyeZnGc20SKyvWy9Skg/edit"
SHEET_NAME = "Codex"
FONT_NAME = "Roboto Mono"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the CGT calculator ETL: PDF to CSV, then CSV to Google Sheets."
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=PDF_PATH,
        help="Path to the Shareworks statement PDF.",
    )
    parser.add_argument(
        "--outputs-dir",
        type=Path,
        default=OUT_DIR,
        help="Directory for generated CSV/audit files.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=WORK_DIR,
        help="Directory for extracted text and FX cache.",
    )
    parser.add_argument(
        "--spreadsheet",
        default=SPREADSHEET_URL,
        help="Google Sheets spreadsheet URL or raw spreadsheet ID.",
    )
    parser.add_argument(
        "--sheet-name",
        default=SHEET_NAME,
        help="Target sheet tab name.",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        help="Optional service account JSON file. Defaults to Application Default Credentials.",
    )
    parser.add_argument(
        "--font-name",
        default=FONT_NAME,
        help="Monospace font family to apply to columns A and K.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear A:K before uploading.",
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Only run extract/transform and leave the generated CSV on disk.",
    )
    return parser.parse_args()


def run_command(command: list[str]) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    subprocess.run(command, check=True)


def main() -> None:
    args = parse_args()
    csv_path = args.outputs_dir / CSV_PATH.name

    extract_command = [
        sys.executable,
        "src/extract_shareworks_statement.py",
        "--pdf",
        str(args.pdf),
        "--outputs-dir",
        str(args.outputs_dir),
        "--work-dir",
        str(args.work_dir),
    ]
    run_command(extract_command)

    if args.skip_load:
        print(f"load_skipped=true")
        print(f"rows_csv={csv_path}")
        return

    load_command = [
        sys.executable,
        "src/upload_to_google_sheets.py",
        "--csv",
        str(csv_path),
        "--spreadsheet",
        args.spreadsheet,
        "--sheet-name",
        args.sheet_name,
        "--font-name",
        args.font_name,
    ]
    if args.credentials:
        load_command.extend(["--credentials", str(args.credentials)])
    if args.no_clear:
        load_command.append("--no-clear")

    run_command(load_command)


if __name__ == "__main__":
    main()
