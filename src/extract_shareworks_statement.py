import argparse
import csv
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from pypdf import PdfReader


PDF_PATH = Path("/Users/bradwright/Downloads/statement.pdf")
OUT_DIR = Path("outputs")
TEXT_PATH = Path("work/statement_raw_text.txt")
FX_CACHE_PATH = Path("work/frankfurter_usd_gbp_rates.json")
FX_API_TEMPLATE = "https://api.frankfurter.dev/v1/{date}?base=USD&symbols=GBP"

DATE_RE = r"\d{2}-[A-Za-z]{3}-\d{4}"
LONG_DATE_RE = r"[A-Za-z]+ \d{2}, \d{4}"
ALLOWED_FEE_COMPONENTS = ["Brokerage Commission", "Supplemental Transaction Fee"]
EXCLUDED_FEE_COMPONENTS = ["Wire Fee", "EFT"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Shareworks RSU activity into CGT calculator-ready spreadsheet rows."
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
        default=Path("work"),
        help="Directory for extracted text and FX cache.",
    )
    return parser.parse_args()


def clean_text(text: str) -> str:
    text = text.replace("\u200b", "")
    text = text.replace("\xa0", " ")
    return text


def parse_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", "").replace("$", "").replace(" USD", "").strip())


def fmt_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def fmt_money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value.quantize(Decimal('0.01'))}"


def fmt_rate(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f").rstrip("0").rstrip(".")


def fmt_calculated(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f").rstrip("0").rstrip(".")


def fixed(value: Decimal | str, places: int) -> str:
    decimal = parse_decimal(value) if isinstance(value, str) else value
    quantizer = Decimal("1").scaleb(-places)
    return f"{decimal.quantize(quantizer, rounding=ROUND_HALF_UP):.{places}f}"


def format_cgt_date(date_value: str) -> str:
    return datetime.strptime(date_value, "%Y-%m-%d").strftime("%d/%m/%Y")


def parse_short_date(value: str) -> str:
    return datetime.strptime(value, "%d-%b-%Y").date().isoformat()


def parse_long_date(value: str) -> str:
    return datetime.strptime(value, "%B %d, %Y").date().isoformat()


def first_match(pattern: str, text: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    return match.group(1) if match else None


def sum_fee_components(section: str, component_names: list[str]) -> tuple[Decimal, dict[str, str]]:
    components: dict[str, str] = {}
    total = Decimal("0")
    for name in component_names:
        match = re.search(rf"\$(-?[\d,]+(?:\.\d+)?) USD\s*{re.escape(name)}", section)
        if match:
            amount = parse_decimal(match.group(1)).copy_abs()
            components[name] = fmt_money(amount)
            total += amount
        else:
            components[name] = ""
    return total, components


def split_sections(text: str, heading_pattern: str) -> list[str]:
    starts = [m.start() for m in re.finditer(heading_pattern, text)]
    sections = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        sections.append(text[start:end])
    return sections


def load_fx_cache() -> dict[str, dict[str, str]]:
    if not FX_CACHE_PATH.exists():
        return {}
    return json.loads(FX_CACHE_PATH.read_text(encoding="utf-8"))


def save_fx_cache(cache: dict[str, dict[str, str]]) -> None:
    FX_CACHE_PATH.parent.mkdir(exist_ok=True)
    FX_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def fetch_frankfurter_rate(lookup_date: str) -> dict[str, str]:
    url = FX_API_TEMPLATE.format(date=lookup_date)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"Unable to fetch FX rate for {lookup_date}") from last_error
    return {
        "requested_date": lookup_date,
        "rate": fmt_rate(payload["rates"]["GBP"]),
        "rate_date": payload["date"],
        "source": "Frankfurter USD->GBP",
        "url": url,
    }


def get_fx_rates(lookup_dates: list[str]) -> dict[str, dict[str, str]]:
    cache = load_fx_cache()
    missing_dates = [date for date in lookup_dates if date not in cache]
    for index, lookup_date in enumerate(missing_dates):
        cache[lookup_date] = fetch_frankfurter_rate(lookup_date)
        save_fx_cache(cache)
        if index < len(missing_dates) - 1:
            time.sleep(0.2)
    return {date: cache[date] for date in lookup_dates}


def apply_fx_rates(rows: list[dict[str, str]]) -> None:
    lookup_dates = sorted({row["_fx_lookup_date"] for row in rows if row.get("_fx_lookup_date")})
    rates = get_fx_rates(lookup_dates)
    for row in rows:
        info = rates[row["_fx_lookup_date"]]
        row["FX Rate"] = info["rate"]
        row["_fx_rate_date"] = info["rate_date"]
        row["_fx_source"] = info["source"]


def apply_derived_values(rows: list[dict[str, str]]) -> None:
    running_total = Decimal("0")
    for row in rows:
        quantity = parse_decimal(row["Quantity"])
        if row["Type"] == "Release":
            running_total += quantity
        else:
            running_total -= quantity

        usd_price = parse_decimal(row["USD Price"])
        usd_fees = parse_decimal(row["USD Fees"]) if row["USD Fees"] else Decimal("0")
        fx_rate = parse_decimal(row["FX Rate"])

        row["Running total"] = fmt_calculated(running_total)
        row["GBP Price"] = fmt_calculated(usd_price * fx_rate)
        row["GBP Fees"] = fmt_calculated(usd_fees * fx_rate)
        row["CGT Calculator String"] = (
            ("B" if row["Type"] == "Release" else "S")
            + f" {format_cgt_date(row['Date'])}"
            + f" SHOP {fixed(row['Quantity'], 6)}"
            + f" {fixed(row['GBP Price'], 4)}"
            + f" {fixed(row['GBP Fees'], 2)}"
            + " 0.00"
        )


def enrich_audit_with_fx(audit: list[dict[str, str]], rows: list[dict[str, str]]) -> None:
    by_id_type = {(row["ID"], row["Type"]): row for row in rows}
    for entry in audit:
        if entry["source_type"] == "Release section":
            release = by_id_type.get((entry["id"], "Release"))
            sell_to_cover = by_id_type.get((entry["id"], "Sell to Cover"))
            entry["fx_rate_for_spreadsheet"] = (
                f"Release row: {release['FX Rate']} ({release['_fx_rate_date']}); "
                f"Sell to Cover row: {sell_to_cover['FX Rate']} ({sell_to_cover['_fx_rate_date']})"
            )
            entry["fx_rate_source"] = "Frankfurter USD->GBP"
        elif entry["source_type"] == "Withdrawal section":
            withdrawal = by_id_type.get((entry["id"], "Withdrawal"))
            entry["fx_rate_for_spreadsheet"] = f"{withdrawal['FX Rate']} ({withdrawal['_fx_rate_date']})"
            entry["fx_rate_source"] = "Frankfurter USD->GBP"
        else:
            entry["fx_rate_for_spreadsheet"] = ""
            entry["fx_rate_source"] = ""


def parse_releases(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    for source_order, section in enumerate(split_sections(text, r"Share Units - Release \(")):
        release_id = first_match(r"Share Units - Release \(([^)]+)\)", section)
        settlement_date = first_match(rf"({DATE_RE})\s*Settlement Date:", section)
        release_date = first_match(rf"({DATE_RE})\s*Release Date:", section)
        quantity_released = first_match(r"([\d,]+(?:\.\d+)?)\s*Quantity Released:", section)
        release_price = first_match(r"\$([\d,]+(?:\.\d+)?) USD\s*Release Price:", section)
        quantity_sold = first_match(r"([\d,]+(?:\.\d+)?)\s*Number of Restricted Awards Sold:", section)
        sale_price = first_match(r"\$([\d,]+(?:\.\d+)?) USD\s*Sale Price:", section)
        gross_release_value = first_match(r"\$([\d,]+(?:\.\d+)?) USD\s*Gross Release Value:", section)
        fee_total, fee_components = sum_fee_components(section, ALLOWED_FEE_COMPONENTS)

        required = {
            "release_id": release_id,
            "settlement_date": settlement_date,
            "release_date": release_date,
            "quantity_released": quantity_released,
            "release_price": release_price,
            "quantity_sold": quantity_sold,
            "sale_price": sale_price,
        }
        missing = [key for key, value in required.items() if value is None]
        status = "ok" if not missing else f"missing: {', '.join(missing)}"

        if missing:
            audit.append(
                {
                    "source_type": "Release section",
                    "id": release_id or "",
                    "status": status,
                    "transaction_date": parse_short_date(release_date) if release_date else "",
                    "date_source": "Release Date",
                    "settlement_date": parse_short_date(settlement_date) if settlement_date else "",
                    "fx_lookup_date_for_spreadsheet": "",
                    "quantity_released": quantity_released or "",
                    "quantity_sold": quantity_sold or "",
                    "usd_price": release_price or sale_price or "",
                    "usd_fees": fmt_money(fee_total),
                    "brokerage_commission": fee_components["Brokerage Commission"],
                    "supplemental_transaction_fee": fee_components["Supplemental Transaction Fee"],
                    "wire_fee_excluded": "",
                    "gross_value_or_proceeds": gross_release_value or "",
                    "notes": status,
                }
            )
            continue

        rows.append(
            {
                "ID": release_id,
                "Date": parse_short_date(release_date),
                "Type": "Release",
                "Quantity": quantity_released,
                "USD Price": release_price,
                "USD Fees": "",
                "FX Rate": "",
                "_source_order": source_order,
                "_sub_order": 0,
                "_fx_lookup_date": parse_short_date(release_date),
            }
        )
        rows.append(
            {
                "ID": release_id,
                "Date": parse_short_date(release_date),
                "Type": "Sell to Cover",
                "Quantity": quantity_sold,
                "USD Price": sale_price,
                "USD Fees": fmt_money(fee_total),
                "FX Rate": "",
                "_source_order": source_order,
                "_sub_order": 1,
                "_fx_lookup_date": parse_short_date(settlement_date),
            }
        )
        audit.append(
            {
                "source_type": "Release section",
                "id": release_id,
                "status": status,
                "transaction_date": parse_short_date(release_date),
                "date_source": "Release Date",
                "settlement_date": parse_short_date(settlement_date),
                "fx_lookup_date_for_spreadsheet": f"Release row: {parse_short_date(release_date)}; Sell to Cover row: {parse_short_date(settlement_date)}",
                "quantity_released": quantity_released,
                "quantity_sold": quantity_sold,
                "usd_price": release_price,
                "usd_fees": fmt_money(fee_total),
                "brokerage_commission": fee_components["Brokerage Commission"],
                "supplemental_transaction_fee": fee_components["Supplemental Transaction Fee"],
                "wire_fee_excluded": "",
                "gross_value_or_proceeds": gross_release_value or "",
                "notes": "Release FX lookup date is release_date; Sell to Cover FX lookup date is settlement_date.",
            }
        )
    return rows, audit


def parse_withdrawals(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    for source_order, section in enumerate(split_sections(text, r"Withdrawal on ")):
        withdrawal_date_long = first_match(rf"Withdrawal on ({LONG_DATE_RE})", section)
        reference_number = first_match(r"([A-Z0-9-]+)\s*Reference Number:", section)
        settlement_date = first_match(rf"({DATE_RE})\s*Settlement Date:", section)
        usd_price = first_match(r"\$([\d,]+(?:\.\d+)?) USD\s*Market Price Per Unit:", section)
        shares_sold = first_match(r"([\d,]+(?:\.\d+)?)\s*Shares Sold:", section)
        gross_proceeds = first_match(r"\$([\d,]+(?:\.\d+)?) USD\s*Gross Proceeds", section)
        wire_fee = first_match(r"\$(-?[\d,]+(?:\.\d+)?) USD\s*Wire Fee", section)
        fee_total, fee_components = sum_fee_components(section, ALLOWED_FEE_COMPONENTS)

        required = {
            "withdrawal_date": withdrawal_date_long,
            "reference_number": reference_number,
            "settlement_date": settlement_date,
            "usd_price": usd_price,
            "shares_sold": shares_sold,
        }
        missing = [key for key, value in required.items() if value is None]
        status = "ok" if not missing else f"missing: {', '.join(missing)}"

        withdrawal_date = parse_long_date(withdrawal_date_long) if withdrawal_date_long else ""
        settlement_date_iso = parse_short_date(settlement_date) if settlement_date else ""
        if withdrawal_date and settlement_date_iso and withdrawal_date == settlement_date_iso:
            status = "check: withdrawal date equals settlement date"
        if missing:
            audit.append(
                {
                    "source_type": "Withdrawal section",
                    "id": reference_number or "",
                    "status": status,
                    "transaction_date": withdrawal_date,
                    "date_source": "Withdrawal heading",
                    "settlement_date": settlement_date_iso,
                    "fx_lookup_date_for_spreadsheet": withdrawal_date,
                    "quantity_released": "",
                    "quantity_sold": shares_sold or "",
                    "usd_price": usd_price or "",
                    "usd_fees": fmt_money(fee_total),
                    "brokerage_commission": fee_components["Brokerage Commission"],
                    "supplemental_transaction_fee": fee_components["Supplemental Transaction Fee"],
                    "wire_fee_excluded": fmt_money(parse_decimal(wire_fee).copy_abs()) if wire_fee else "",
                    "gross_value_or_proceeds": gross_proceeds or "",
                    "notes": status,
                }
            )
            continue

        rows.append(
            {
                "ID": reference_number,
                "Date": withdrawal_date,
                "Type": "Withdrawal",
                "Quantity": shares_sold,
                "USD Price": usd_price,
                "USD Fees": fmt_money(fee_total),
                "FX Rate": "",
                "_source_order": source_order,
                "_sub_order": 2,
                "_fx_lookup_date": withdrawal_date,
            }
        )
        audit.append(
            {
                "source_type": "Withdrawal section",
                "id": reference_number,
                "status": status,
                "transaction_date": withdrawal_date,
                "date_source": "Withdrawal heading",
                "settlement_date": settlement_date_iso,
                "fx_lookup_date_for_spreadsheet": withdrawal_date,
                "quantity_released": "",
                "quantity_sold": shares_sold,
                "usd_price": usd_price,
                "usd_fees": fmt_money(fee_total),
                "brokerage_commission": fee_components["Brokerage Commission"],
                "supplemental_transaction_fee": fee_components["Supplemental Transaction Fee"],
                "wire_fee_excluded": fmt_money(parse_decimal(wire_fee).copy_abs()) if wire_fee else "",
                "gross_value_or_proceeds": gross_proceeds or "",
                "notes": "Date and FX lookup date come from the Withdrawal on ... heading, not Settlement Date. Wire/EFT fees excluded. Brokerage commission and supplemental transaction fee included.",
            }
        )
    return rows, audit


def sort_key(row: dict[str, str]) -> tuple[str, int, int, str]:
    same_day_group = 1 if row["Type"] == "Withdrawal" else 0
    return (row["Date"], same_day_group, int(row["_source_order"]), row["ID"])


def main() -> None:
    global PDF_PATH, OUT_DIR, TEXT_PATH, FX_CACHE_PATH

    args = parse_args()
    PDF_PATH = args.pdf
    OUT_DIR = args.outputs_dir
    TEXT_PATH = args.work_dir / "statement_raw_text.txt"
    FX_CACHE_PATH = args.work_dir / "frankfurter_usd_gbp_rates.json"

    OUT_DIR.mkdir(exist_ok=True)
    TEXT_PATH.parent.mkdir(exist_ok=True)
    reader = PdfReader(str(PDF_PATH))
    text = clean_text("\n\n".join((page.extract_text() or "") for page in reader.pages))
    TEXT_PATH.write_text(text, encoding="utf-8")

    release_rows, release_audit = parse_releases(text)
    withdrawal_rows, withdrawal_audit = parse_withdrawals(text)
    rows = sorted(release_rows + withdrawal_rows, key=sort_key)
    apply_fx_rates(rows)
    apply_derived_values(rows)
    audit = release_audit + withdrawal_audit
    enrich_audit_with_fx(audit, rows)
    public_fieldnames = [
        "ID",
        "Date",
        "Type",
        "Quantity",
        "Running total",
        "USD Price",
        "USD Fees",
        "FX Rate",
        "GBP Price",
        "GBP Fees",
        "CGT Calculator String",
    ]
    public_rows = [
        {key: row[key] for key in public_fieldnames}
        for row in rows
    ]

    csv_path = OUT_DIR / "shareworks_extracted_rows.csv"
    audit_path = OUT_DIR / "shareworks_extraction_audit.csv"

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=public_fieldnames)
        writer.writeheader()
        writer.writerows(public_rows)

    with audit_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "source_type",
            "id",
            "status",
            "transaction_date",
            "date_source",
            "settlement_date",
            "fx_lookup_date_for_spreadsheet",
            "fx_rate_for_spreadsheet",
            "fx_rate_source",
            "quantity_released",
            "quantity_sold",
            "usd_price",
            "usd_fees",
            "brokerage_commission",
            "supplemental_transaction_fee",
            "wire_fee_excluded",
            "gross_value_or_proceeds",
            "notes",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(audit)

    print(f"pages={len(reader.pages)}")
    print(f"release_sections={len(release_audit)}")
    print(f"withdrawal_sections={len(withdrawal_audit)}")
    print(f"spreadsheet_rows={len(rows)}")
    print(f"fx_rates={len({row['_fx_lookup_date'] for row in rows})}")
    print(f"rows_csv={csv_path}")
    print(f"audit_csv={audit_path}")
    failed = [row for row in audit if row["status"] != "ok"]
    print(f"failed_sections={len(failed)}")
    if failed:
        for row in failed:
            print(f"FAILED {row['source_type']} {row['id']}: {row['status']}")


if __name__ == "__main__":
    main()
