import argparse
import csv
import re
from pathlib import Path
from typing import Any

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build


CSV_PATH = Path("outputs/shareworks_extracted_rows.csv")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1MlPi94AqW5Ua5zPn5jPh_q_BiyeZnGc20SKyvWy9Skg/edit"
SHEET_NAME = "Codex"
FONT_NAME = "Roboto Mono"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload extracted CGT calculator rows from CSV into Google Sheets."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=CSV_PATH,
        help="CSV file to upload.",
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
    return parser.parse_args()


def spreadsheet_id_from(value: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", value)
    return match.group(1) if match else value


def quote_sheet_name(sheet_name: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def load_csv_rows(csv_path: Path) -> list[list[str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def build_sheets_service(credentials_path: Path | None) -> Any:
    if credentials_path:
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=SCOPES,
        )
    else:
        credentials, _ = google.auth.default(scopes=SCOPES)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def get_sheet_id(service: Any, spreadsheet_id: str, sheet_name: str) -> int:
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute()
    )
    for sheet in response["sheets"]:
        properties = sheet["properties"]
        if properties["title"] == sheet_name:
            return int(properties["sheetId"])
    available = ", ".join(sheet["properties"]["title"] for sheet in response["sheets"])
    raise ValueError(f"Sheet tab not found: {sheet_name}. Available tabs: {available}")


def upload_rows(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    sheet_id: int,
    rows: list[list[str]],
    font_name: str,
    clear_existing: bool,
) -> None:
    quoted_sheet = quote_sheet_name(sheet_name)
    if clear_existing:
        (
            service.spreadsheets()
            .values()
            .clear(spreadsheetId=spreadsheet_id, range=f"{quoted_sheet}!A:K")
            .execute()
        )

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{quoted_sheet}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        )
        .execute()
    )

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 11,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "bold": True,
                                }
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "fontFamily": font_name,
                                }
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.fontFamily",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startColumnIndex": 10,
                            "endColumnIndex": 11,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "fontFamily": font_name,
                                }
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.fontFamily",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {
                                "frozenRowCount": 1,
                            },
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]
        },
    ).execute()


def main() -> None:
    args = parse_args()
    spreadsheet_id = spreadsheet_id_from(args.spreadsheet)
    rows = load_csv_rows(args.csv)
    service = build_sheets_service(args.credentials)
    sheet_id = get_sheet_id(service, spreadsheet_id, args.sheet_name)

    upload_rows(
        service=service,
        spreadsheet_id=spreadsheet_id,
        sheet_name=args.sheet_name,
        sheet_id=sheet_id,
        rows=rows,
        font_name=args.font_name,
        clear_existing=not args.no_clear,
    )

    print(f"uploaded_rows={len(rows) - 1}")
    print(f"spreadsheet_id={spreadsheet_id}")
    print(f"sheet_name={args.sheet_name}")
    print(f"csv={args.csv}")


if __name__ == "__main__":
    main()
