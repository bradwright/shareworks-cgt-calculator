# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pypdf",
# ]
# ///

import argparse
import csv
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path

from pypdf import PdfReader


OUT_DIR = Path("outputs")
TEXT_PATH = Path("work/statement_raw_text.txt")
FX_CACHE_PATH = Path("work/frankfurter_usd_gbp_rates.json")
FX_API_TEMPLATE = "https://api.frankfurter.dev/v1/{date}?base=USD&symbols=GBP"

DATE_RE = r"\d{2}-[A-Za-z]{3}-\d{4}"
LONG_DATE_RE = r"[A-Za-z]+ \d{2}, \d{4}"
NUMBER_RE = r"-?[\d,]+(?:\.\d+)?"
SHARE_TOLERANCE = Decimal("0.000001")
MONEY_TOLERANCE = Decimal("0.01")
TRANSACTION_HEADING_RE = r"(?m)^[ \t]*(?:Share Units - Release \(|Withdrawal on )"
ALLOWED_FEE_COMPONENTS = ["Brokerage Commission", "Supplemental Transaction Fee"]
EXCLUDED_FEE_COMPONENTS = ["Wire Fee", "EFT"]
PUBLIC_FIELDNAMES = [
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
AUDIT_FIELDNAMES = [
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
    "opening_shares",
    "closing_shares",
    "calculated_closing_shares",
    "minimum_running_shares",
    "balance_difference",
    "notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Shareworks RSU activity into CGT calculator-ready spreadsheet rows."
    )
    parser.add_argument(
        "--shareworks-report",
        "--in",
        "--pdf",
        dest="report",
        type=Path,
        required=True,
        help="Path to the Shareworks statement PDF or saved Account Summary HTML.",
    )
    parser.add_argument(
        "--cgt-symbol",
        required=True,
        help=(
            "Security identifier to emit in CGTCalculator rows. Use an uppercase "
            "one-word identifier and verify it matches the single fund in the statement."
        ),
    )
    parser.add_argument(
        "--outputs-dir",
        type=Path,
        default=OUT_DIR,
        help="Directory for generated CSV/audit files.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional output path for the generated rows CSV.",
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


def clean_cell_text(text: str) -> str:
    return clean_text(" ".join(text.split())).strip()


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


def money_amount(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\$(-?[\d,]+(?:\.\d+)?) USD", value)
    return match.group(1) if match else None


def fmt_abs_money(value: str | None) -> str:
    amount = money_amount(value)
    return fmt_money(parse_decimal(amount).copy_abs()) if amount else ""


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


def split_sections(
    text: str,
    heading_pattern: str,
    boundary_pattern: str | None = None,
) -> list[str]:
    starts = [m.start() for m in re.finditer(heading_pattern, text)]
    boundaries = [
        match.start()
        for match in re.finditer(boundary_pattern or heading_pattern, text)
    ]
    sections = []
    for start in starts:
        end = next((boundary for boundary in boundaries if boundary > start), len(text))
        sections.append(text[start:end])
    return sections


def fund_metadata(fund_names: list[str]) -> dict[str, str]:
    unique_funds = sorted(
        {clean_cell_text(name) for name in fund_names if clean_cell_text(name)}
    )
    return {
        "fund_count": str(len(unique_funds)),
        "fund_names": json.dumps(unique_funds),
    }


def parse_pdf_fund_metadata(text: str) -> dict[str, str]:
    fund_names = [
        match.group(1)
        for match in re.finditer(r"(?mi)^\s*Fund:\s*(.+?)\s*$", text)
    ]
    return fund_metadata(fund_names)


def parse_html_fund_metadata(tables: list[list[list[str]]]) -> dict[str, str]:
    fund_names: list[str] = []
    for table in tables:
        for row in table:
            for index, raw_cell in enumerate(row):
                cell = clean_cell_text(raw_cell)
                inline_match = re.fullmatch(r"Fund\s*:\s*(.+)", cell, re.IGNORECASE)
                if inline_match:
                    fund_names.append(inline_match.group(1))
                elif normalize_label(cell).casefold() == "fund" and index + 1 < len(row):
                    fund_names.append(row[index + 1])
    return fund_metadata(fund_names)


def build_security_validation(
    metadata: dict[str, str],
    symbol: str,
) -> dict[str, str]:
    encoded_funds = metadata.get("fund_names", "")
    try:
        parsed_funds = json.loads(encoded_funds) if encoded_funds else []
    except json.JSONDecodeError as error:
        parsed_funds = []
        status = f"unparseable: statement fund metadata ({error})"
    else:
        if not isinstance(parsed_funds, list) or not all(
            isinstance(fund, str) for fund in parsed_funds
        ):
            parsed_funds = []
            status = "unparseable: statement fund metadata is not a list of strings"
        elif not parsed_funds:
            status = "missing: statement fund"
        elif len(parsed_funds) > 1:
            status = f"ambiguous: found multiple statement funds {parsed_funds}"
        elif not re.fullmatch(r"[A-Z][A-Z0-9.-]*", symbol):
            status = f"invalid: CGT symbol {symbol!r}"
        else:
            status = "ok"

    return {
        "source_type": "Security validation",
        "id": symbol,
        "status": status,
        "notes": (
            f"User-supplied CGT symbol {symbol!r} is emitted only when exactly one "
            f"statement fund is present; parsed funds={parsed_funds}."
        ),
    }


def gross_value_problem(
    quantity: str,
    unit_price: str,
    reported_gross: str,
    label: str,
) -> str | None:
    expected_gross = parse_decimal(quantity) * parse_decimal(unit_price)
    actual_gross = parse_decimal(reported_gross)
    if abs(expected_gross - actual_gross) <= MONEY_TOLERANCE:
        return None
    return (
        f"mismatch: {label} {fmt_decimal(actual_gross)} != "
        f"quantity x price {fmt_decimal(expected_gross)}"
    )


def parse_pdf_holding_summary(text: str) -> dict[str, str]:
    """Extract opening and closing holdings from the PDF Activity table."""
    summary: dict[str, str] = {}
    for prefix, label in [("opening", "Opening Value"), ("closing", "Closing Value")]:
        snapshot_count = len(re.findall(re.escape(label), text))
        summary[f"{prefix}_snapshot_count"] = str(snapshot_count)
        if snapshot_count != 1:
            continue

        matching_line = next(line for line in text.splitlines() if label in line)

        match = re.search(
            rf"\$(?P<market_value>{NUMBER_RE})\s*USD\s*"
            rf"\$(?P<book_value>{NUMBER_RE})\s*USD\s*"
            rf"\$(?P<share_price>{NUMBER_RE})\s*USD\s*"
            rf"(?P<shares>{NUMBER_RE})\s*"
            rf"\$(?P<cash>{NUMBER_RE})\s*USD\s*"
            rf"{re.escape(label)}\s*(?P<date>{DATE_RE})",
            matching_line,
        )
        if not match:
            continue

        summary[f"{prefix}_shares"] = fmt_decimal(parse_decimal(match.group("shares")))
        summary[f"{prefix}_date"] = parse_short_date(match.group("date"))
        summary[f"{prefix}_market_value_usd"] = fmt_money(parse_decimal(match.group("market_value")))
        summary[f"{prefix}_book_value_usd"] = fmt_money(parse_decimal(match.group("book_value")))
        summary[f"{prefix}_share_price_usd"] = fmt_decimal(parse_decimal(match.group("share_price")))
    return summary


class StatementTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg"}:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
            return
        if self._skip_stack:
            return
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(clean_cell_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if not self._skip_stack and self._cell is not None:
            self._cell.append(data)


def normalize_label(value: str) -> str:
    return clean_cell_text(value).rstrip(":")


def table_title(table: list[list[str]]) -> str:
    return table[0][0] if table and table[0] else ""


def field_pairs(table: list[list[str]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in table[1:]:
        if len(row) >= 2:
            fields[normalize_label(row[0])] = row[1]
        if len(row) >= 4:
            fields[normalize_label(row[2])] = row[3]
    return fields


def two_column_pairs(table: list[list[str]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in table[1:]:
        if len(row) >= 2:
            fields[normalize_label(row[0])] = row[1]
    return fields


def table_text(table: list[list[str]]) -> str:
    return "\n".join(" | ".join(cell for cell in row if cell) for row in table if any(row))


def parse_html_holding_summary(tables: list[list[list[str]]]) -> dict[str, str]:
    """Extract opening and closing holdings from a saved HTML Activity table."""
    snapshots: dict[str, list[dict[str, str]]] = {"opening": [], "closing": []}
    snapshot_counts = {"opening": 0, "closing": 0}

    for table in tables:
        for header_row_index, header_row in enumerate(table):
            normalized_headers = [normalize_label(cell).casefold() for cell in header_row]
            required_headers = ["number of shares", "activity", "entry date"]
            if not all(header in normalized_headers for header in required_headers):
                continue

            header_indexes = {
                "shares": normalized_headers.index("number of shares"),
                "activity": normalized_headers.index("activity"),
                "date": normalized_headers.index("entry date"),
            }
            for field_name, heading in [
                ("market_value", "market value"),
                ("book_value", "book value"),
                ("share_price", "share price"),
            ]:
                if heading in normalized_headers:
                    header_indexes[field_name] = normalized_headers.index(heading)

            for row in table[header_row_index + 1 :]:
                if header_indexes["activity"] >= len(row):
                    continue
                activity = normalize_label(row[header_indexes["activity"]]).casefold()
                if activity not in {"opening value", "closing value"}:
                    continue

                prefix = "opening" if activity == "opening value" else "closing"
                snapshot_counts[prefix] += 1
                if max(header_indexes["shares"], header_indexes["date"]) >= len(row):
                    continue

                shares_value = row[header_indexes["shares"]].replace(" ", "")
                date_match = re.search(DATE_RE, row[header_indexes["date"]])
                if not re.fullmatch(NUMBER_RE, shares_value) or not date_match:
                    continue

                snapshot = {
                    "shares": fmt_decimal(parse_decimal(shares_value)),
                    "date": parse_short_date(date_match.group(0)),
                }
                for field_name in ["market_value", "book_value", "share_price"]:
                    field_index = header_indexes.get(field_name)
                    if field_index is None or field_index >= len(row):
                        continue
                    amount = money_amount(row[field_index])
                    if amount is not None:
                        value = parse_decimal(amount)
                        formatter = fmt_decimal if field_name == "share_price" else fmt_money
                        snapshot[f"{field_name}_usd"] = formatter(value)
                snapshots[prefix].append(snapshot)
            break

    summary: dict[str, str] = {}
    for prefix, matches in snapshots.items():
        summary[f"{prefix}_snapshot_count"] = str(snapshot_counts[prefix])
        if snapshot_counts[prefix] == 1 and len(matches) == 1:
            for field_name, value in matches[0].items():
                summary[f"{prefix}_{field_name}"] = value
    return summary


def activity_metadata(
    prefix: str,
    status: str,
    candidate_count: int,
    events: list[dict[str, str]],
) -> dict[str, str]:
    return {
        f"{prefix}_status": status,
        f"{prefix}_candidate_count": str(candidate_count),
        f"{prefix}_events": json.dumps(events, sort_keys=True),
    }


def parse_pdf_holding_activity(text: str) -> dict[str, str]:
    """Parse the independent Stock/Shares Holdings Activity movement ledger."""
    lines = text.splitlines()
    opening_indexes = [index for index, line in enumerate(lines) if "Opening Value" in line]
    closing_indexes = [index for index, line in enumerate(lines) if "Closing Value" in line]
    if (
        text.count("Opening Value") != 1
        or text.count("Closing Value") != 1
        or len(opening_indexes) != 1
        or len(closing_indexes) != 1
        or opening_indexes[0] >= closing_indexes[0]
    ):
        return activity_metadata(
            "holding_activity",
            "unparseable: unique ordered opening and closing Activity rows are required",
            0,
            [],
        )

    events: list[dict[str, str]] = []
    unrecognized: list[str] = []
    movement_pattern = re.compile(
        rf"^.*?(?P<quantity>{NUMBER_RE})\s+"
        rf"(?P<source>[A-Za-z][A-Za-z &/'-]*?)"
        rf"(?P<activity>Release|Sale)"
        rf"(?:\s*\((?P<id>[^)]+)\))?"
        rf"(?P<date>{DATE_RE})\s*$"
    )
    for raw_line in lines[opening_indexes[0] + 1 : closing_indexes[0]]:
        line = raw_line.strip()
        if not line or re.fullmatch(r"Page \d+", line):
            continue
        if line == "Activity" or line.startswith("Market Value") or line.startswith("Fund:"):
            continue

        match = movement_pattern.fullmatch(line)
        if not match:
            unrecognized.append(line[:160])
            continue

        activity_type = match.group("activity")
        event_id = (match.group("id") or "").strip()
        signed_quantity = parse_decimal(match.group("quantity"))
        valid_sign = signed_quantity > 0 if activity_type == "Release" else signed_quantity < 0
        if not valid_sign or (activity_type == "Release" and not event_id):
            unrecognized.append(line[:160])
            continue
        events.append(
            {
                "type": activity_type,
                "id": event_id,
                "date": parse_short_date(match.group("date")),
                "quantity": fmt_decimal(signed_quantity.copy_abs()),
            }
        )

    status = "ok"
    if unrecognized:
        status = (
            f"unsupported: {len(unrecognized)} unrecognized holding Activity row(s): "
            + " | ".join(unrecognized[:3])
        )
    return activity_metadata(
        "holding_activity",
        status,
        len(events) + len(unrecognized),
        events,
    )


def parse_pdf_rsu_activity(text: str) -> dict[str, str]:
    """Parse release triplets from the independent RSU Activity ledger."""
    lines = text.splitlines()
    heading_indexes = [
        index for index, line in enumerate(lines) if "Share Units (RSU) - Activity" in line
    ]
    if len(heading_indexes) != 1:
        return activity_metadata(
            "rsu_activity",
            f"ambiguous: found {len(heading_indexes)} RSU Activity sections",
            0,
            [],
        )
    ending_indexes = [
        index
        for index in range(heading_indexes[0] + 1, len(lines))
        if "Ending balance" in lines[index]
    ]
    if len(ending_indexes) != 1:
        return activity_metadata(
            "rsu_activity",
            f"ambiguous: found {len(ending_indexes)} RSU Activity ending rows",
            0,
            [],
        )

    block = lines[heading_indexes[0] + 1 : ending_indexes[0]]
    candidate_indexes: list[int] = []
    grant_pattern = re.compile(r"\d{4}-[A-Za-z0-9-]+-RSU")
    for index, raw_line in enumerate(block):
        line = raw_line.strip()
        grant_match = grant_pattern.search(line)
        values = re.findall(NUMBER_RE, line[: grant_match.start()]) if grant_match else []
        has_negative_quantity = any(parse_decimal(value) < 0 for value in values)
        if re.search(r"Release\s*$", line) or has_negative_quantity:
            candidate_indexes.append(index)
    events: list[dict[str, str]] = []
    malformed: list[str] = []
    release_pattern = re.compile(
        rf"^(?P<values>.*?)(?P<grant>\d{{4}}-[A-Za-z0-9-]+-RSU)Release\s*$"
    )
    for index in candidate_indexes:
        release_match = release_pattern.fullmatch(block[index].strip())
        id_match = (
            re.fullmatch(r"\(([^)]+)\)\s*", block[index + 1].strip())
            if index + 1 < len(block)
            else None
        )
        date_match = (
            re.fullmatch(DATE_RE, block[index + 2].strip())
            if index + 2 < len(block)
            else None
        )
        values = re.findall(NUMBER_RE, release_match.group("values")) if release_match else []
        signed_quantity = parse_decimal(values[0]) if values else None
        if (
            release_match is None
            or id_match is None
            or date_match is None
            or signed_quantity is None
            or signed_quantity >= 0
        ):
            malformed.append(block[index].strip()[:160])
            continue
        events.append(
            {
                "type": "Release",
                "id": id_match.group(1).strip(),
                "date": parse_short_date(date_match.group(0)),
                "quantity": fmt_decimal(signed_quantity.copy_abs()),
            }
        )

    status = "ok"
    if malformed:
        status = (
            f"unparseable: {len(malformed)} malformed RSU Activity release row(s): "
            + " | ".join(malformed[:3])
        )
    return activity_metadata("rsu_activity", status, len(candidate_indexes), events)


def find_activity_headers(
    tables: list[list[list[str]]],
    required_headers: list[str],
) -> list[tuple[list[list[str]], int, list[str]]]:
    matches: list[tuple[list[list[str]], int, list[str]]] = []
    for table in tables:
        for row_index, row in enumerate(table):
            normalized = [normalize_label(cell).casefold() for cell in row]
            if all(header in normalized for header in required_headers):
                matches.append((table, row_index, normalized))
                break
    return matches


def parse_html_holding_activity(tables: list[list[list[str]]]) -> dict[str, str]:
    """Parse the independent holdings Activity ledger from saved HTML."""
    header_matches = find_activity_headers(
        tables,
        ["number of shares", "activity", "entry date"],
    )
    if len(header_matches) != 1:
        return activity_metadata(
            "holding_activity",
            f"ambiguous: found {len(header_matches)} holdings Activity tables",
            0,
            [],
        )

    table, header_row_index, headers = header_matches[0]
    shares_index = headers.index("number of shares")
    activity_index = headers.index("activity")
    date_index = headers.index("entry date")
    data_rows = table[header_row_index + 1 :]
    opening_indexes = [
        index
        for index, row in enumerate(data_rows)
        if activity_index < len(row)
        and normalize_label(row[activity_index]).casefold() == "opening value"
    ]
    closing_indexes = [
        index
        for index, row in enumerate(data_rows)
        if activity_index < len(row)
        and normalize_label(row[activity_index]).casefold() == "closing value"
    ]
    if (
        len(opening_indexes) != 1
        or len(closing_indexes) != 1
        or opening_indexes[0] >= closing_indexes[0]
    ):
        return activity_metadata(
            "holding_activity",
            "unparseable: unique ordered opening and closing holdings Activity rows are required",
            0,
            [],
        )

    events: list[dict[str, str]] = []
    unrecognized: list[str] = []
    candidate_count = 0
    for row in data_rows[opening_indexes[0] + 1 : closing_indexes[0]]:
        if not any(cell.strip() for cell in row):
            continue
        candidate_count += 1
        if activity_index >= len(row):
            unrecognized.append(" | ".join(row)[:160])
            continue
        activity = normalize_label(row[activity_index])
        if not activity:
            unrecognized.append(" | ".join(row)[:160])
            continue
        if activity.casefold() == "activity":
            candidate_count -= 1
            continue
        if max(shares_index, date_index) >= len(row):
            unrecognized.append(" | ".join(row)[:160])
            continue
        shares_text = row[shares_index].replace(" ", "")
        date_match = re.search(DATE_RE, row[date_index])
        release_match = re.fullmatch(r"Release\s*\(([^)]+)\)", activity, re.IGNORECASE)
        sale_match = re.fullmatch(r"Sale(?:\s*\(([^)]+)\))?", activity, re.IGNORECASE)
        if not re.fullmatch(NUMBER_RE, shares_text) or not date_match or not (release_match or sale_match):
            unrecognized.append(" | ".join(row)[:160])
            continue
        signed_quantity = parse_decimal(shares_text)
        event_type = "Release" if release_match else "Sale"
        valid_sign = signed_quantity > 0 if event_type == "Release" else signed_quantity < 0
        if not valid_sign:
            unrecognized.append(" | ".join(row)[:160])
            continue
        events.append(
            {
                "type": event_type,
                "id": release_match.group(1).strip() if release_match else "",
                "date": parse_short_date(date_match.group(0)),
                "quantity": fmt_decimal(signed_quantity.copy_abs()),
            }
        )

    status = "ok"
    if unrecognized:
        status = (
            f"unsupported: {len(unrecognized)} unrecognized holding Activity row(s): "
            + " | ".join(unrecognized[:3])
        )
    return activity_metadata("holding_activity", status, candidate_count, events)


def parse_html_rsu_activity(tables: list[list[list[str]]]) -> dict[str, str]:
    """Parse release rows from the independent RSU Activity table in saved HTML."""
    header_matches = find_activity_headers(tables, ["grant name", "activity", "date"])
    if len(header_matches) != 1:
        return activity_metadata(
            "rsu_activity",
            f"ambiguous: found {len(header_matches)} RSU Activity tables",
            0,
            [],
        )

    table, header_row_index, headers = header_matches[0]
    activity_index = headers.index("activity")
    date_index = headers.index("date")
    events: list[dict[str, str]] = []
    malformed: list[str] = []
    candidate_count = 0
    for row in table[header_row_index + 1 :]:
        if not any(cell.strip() for cell in row):
            continue
        negative_quantities = [
            parse_decimal(cell)
            for cell in row
            if re.fullmatch(NUMBER_RE, cell.replace(" ", ""))
            and parse_decimal(cell) < 0
        ]
        if activity_index >= len(row):
            candidate_count += 1
            malformed.append(" | ".join(row)[:160])
            continue
        activity = normalize_label(row[activity_index])
        if not activity:
            candidate_count += 1
            malformed.append(" | ".join(row)[:160])
            continue
        if not activity.casefold().startswith("release"):
            if negative_quantities:
                candidate_count += 1
                malformed.append(" | ".join(row)[:160])
            continue
        candidate_count += 1
        joined_row = " | ".join(row)
        id_match = re.search(r"Release\s*\(([^)]+)\)", joined_row, re.IGNORECASE)
        date_match = re.search(DATE_RE, row[date_index]) if date_index < len(row) else None
        if not id_match or not date_match or not negative_quantities:
            malformed.append(joined_row[:160])
            continue
        events.append(
            {
                "type": "Release",
                "id": id_match.group(1).strip(),
                "date": parse_short_date(date_match.group(0)),
                "quantity": fmt_decimal(negative_quantities[0].copy_abs()),
            }
        )

    status = "ok"
    if malformed:
        status = (
            f"unparseable: {len(malformed)} malformed RSU Activity release row(s): "
            + " | ".join(malformed[:3])
        )
    return activity_metadata("rsu_activity", status, candidate_count, events)


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


def calculate_share_balances(
    rows: list[dict[str, str]],
    opening_shares: Decimal = Decimal("0"),
) -> tuple[Decimal, Decimal]:
    running_total = opening_shares
    minimum_running_total = opening_shares
    for row in rows:
        quantity = parse_decimal(row["Quantity"])
        if row["Type"] == "Release":
            running_total += quantity
        else:
            running_total -= quantity
        minimum_running_total = min(minimum_running_total, running_total)
    return running_total, minimum_running_total


def apply_running_totals(
    rows: list[dict[str, str]],
    opening_shares: Decimal = Decimal("0"),
) -> Decimal:
    running_total = opening_shares
    for row in rows:
        quantity = parse_decimal(row["Quantity"])
        running_total += quantity if row["Type"] == "Release" else -quantity
        row["Running total"] = fmt_calculated(running_total)
    return running_total


def apply_derived_values(
    rows: list[dict[str, str]],
    opening_shares: Decimal = Decimal("0"),
    *,
    symbol: str,
) -> Decimal:
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]*", symbol):
        raise ValueError(f"Invalid CGT symbol: {symbol!r}")
    running_total = apply_running_totals(rows, opening_shares)
    for row in rows:

        usd_price = parse_decimal(row["USD Price"])
        usd_fees = parse_decimal(row["USD Fees"]) if row["USD Fees"] else Decimal("0")
        fx_rate = parse_decimal(row["FX Rate"])

        row["GBP Price"] = fmt_calculated(usd_price * fx_rate)
        row["GBP Fees"] = fmt_calculated(usd_fees * fx_rate)
        row["CGT Calculator String"] = (
            ("B" if row["Type"] == "Release" else "S")
            + f" {format_cgt_date(row['Date'])}"
            + f" {symbol} {fixed(row['Quantity'], 6)}"
            + f" {fixed(row['GBP Price'], 4)}"
            + f" {fixed(row['GBP Fees'], 2)}"
            + " 0.00"
        )
    return running_total


def build_holding_reconciliation(
    metadata: dict[str, str],
    calculated_closing_shares: Decimal,
    minimum_running_shares: Decimal,
) -> dict[str, str]:
    """Build an audit row and CGT-readiness status for statement holdings."""
    opening_text = metadata.get("opening_shares", "")
    closing_text = metadata.get("closing_shares", "")
    problems: list[str] = []

    for prefix, value in [("opening", opening_text), ("closing", closing_text)]:
        count_text = metadata.get(f"{prefix}_snapshot_count", "1" if value else "0")
        count = int(count_text) if count_text.isdigit() else 0
        if count == 0:
            problems.append(f"missing: {prefix} holding")
        elif count > 1:
            problems.append(f"ambiguous: found {count} {prefix} holdings")
        elif not value:
            problems.append(f"unparseable: {prefix} holding")

    opening_shares = parse_decimal(opening_text) if opening_text else Decimal("0")
    closing_shares = parse_decimal(closing_text) if closing_text else None
    difference = calculated_closing_shares - closing_shares if closing_shares is not None else None

    if difference is not None and abs(difference) > SHARE_TOLERANCE:
        problems.append(
            "mismatch: calculated closing holding "
            f"{fmt_decimal(calculated_closing_shares)} != reported {fmt_decimal(closing_shares)}"
        )

    if minimum_running_shares < -SHARE_TOLERANCE:
        problems.append(
            f"mismatch: running holding falls below zero ({fmt_decimal(minimum_running_shares)})"
        )

    if opening_text and opening_shares != 0:
        problems.append("incomplete: non-zero opening holding requires earlier acquisition history")

    status = "; ".join(problems) if problems else "ok"
    notes = (
        "Opening shares seed the running-total reconciliation only; no synthetic CGT acquisition is emitted. "
        "Provide the original acquisition history for a non-zero opening holding before using the CGT Calculator strings."
    )
    return {
        "source_type": "Holding reconciliation",
        "id": "",
        "status": status,
        "transaction_date": metadata.get("closing_date", ""),
        "date_source": "Activity opening/closing values",
        "settlement_date": "",
        "fx_lookup_date_for_spreadsheet": "",
        "quantity_released": "",
        "quantity_sold": "",
        "usd_price": "",
        "usd_fees": "",
        "brokerage_commission": "",
        "supplemental_transaction_fee": "",
        "wire_fee_excluded": "",
        "gross_value_or_proceeds": "",
        "opening_shares": opening_text,
        "closing_shares": closing_text,
        "calculated_closing_shares": fmt_decimal(calculated_closing_shares),
        "minimum_running_shares": fmt_decimal(minimum_running_shares),
        "balance_difference": fmt_decimal(difference) if difference is not None else "",
        "notes": notes,
    }


def decode_activity_events(metadata: dict[str, str], prefix: str) -> list[dict[str, str]]:
    encoded = metadata.get(f"{prefix}_events", "")
    parsed = json.loads(encoded) if encoded else []
    if not isinstance(parsed, list) or not all(isinstance(event, dict) for event in parsed):
        raise ValueError(f"{prefix} events are not a list of objects")
    return parsed


def build_activity_reconciliation(
    metadata: dict[str, str],
    detail_audit: list[dict[str, str]],
) -> dict[str, str]:
    """Cross-check independent Activity ledgers against parsed detail sections."""
    problems: list[str] = []
    for prefix, label in [
        ("holding_activity", "holdings Activity"),
        ("rsu_activity", "RSU Activity"),
    ]:
        source_status = metadata.get(f"{prefix}_status", "missing: source was not parsed")
        if source_status != "ok":
            problems.append(f"{label}: {source_status}")

    try:
        holding_events = decode_activity_events(metadata, "holding_activity")
        rsu_events = decode_activity_events(metadata, "rsu_activity")
    except (json.JSONDecodeError, ValueError) as error:
        holding_events = []
        rsu_events = []
        problems.append(f"unparseable: Activity event metadata ({error})")

    for prefix, events in [
        ("holding_activity", holding_events),
        ("rsu_activity", rsu_events),
    ]:
        candidate_text = metadata.get(f"{prefix}_candidate_count", "")
        if not candidate_text.isdigit() or int(candidate_text) != len(events):
            problems.append(
                f"mismatch: {prefix} candidates {candidate_text or 'unknown'} != parsed {len(events)}"
            )

    holding_releases = [event for event in holding_events if event.get("type") == "Release"]
    holding_sales = [event for event in holding_events if event.get("type") == "Sale"]
    rsu_releases = [event for event in rsu_events if event.get("type") == "Release"]
    detail_releases = [
        entry for entry in detail_audit if entry.get("source_type") == "Release section"
    ]
    detail_withdrawals = [
        entry for entry in detail_audit if entry.get("source_type") == "Withdrawal section"
    ]

    def event_map(events: list[dict[str, str]], label: str) -> dict[str, dict[str, str]]:
        ids = [event.get("id", "") for event in events]
        duplicates = sorted(event_id for event_id, count in Counter(ids).items() if not event_id or count > 1)
        if duplicates:
            problems.append(f"ambiguous: duplicate or blank {label} IDs {duplicates}")
        return {event.get("id", ""): event for event in events if event.get("id")}

    holding_release_map = event_map(holding_releases, "holdings Activity release")
    rsu_release_map = event_map(rsu_releases, "RSU Activity release")
    detail_release_map = event_map(detail_releases, "detail release")

    for release_id, detail in detail_release_map.items():
        quantity_released = detail.get("quantity_released", "")
        quantity_sold = detail.get("quantity_sold", "")
        if not quantity_released or not quantity_sold:
            continue
        gross_quantity = parse_decimal(quantity_released)
        net_quantity = gross_quantity - parse_decimal(quantity_sold)

        rsu_event = rsu_release_map.get(release_id)
        if rsu_event is None:
            problems.append(f"missing: RSU Activity release {release_id}")
        elif (
            rsu_event.get("date") != detail.get("transaction_date")
            or abs(parse_decimal(rsu_event.get("quantity", "0")) - gross_quantity) > SHARE_TOLERANCE
        ):
            problems.append(f"mismatch: RSU Activity release {release_id}")

        holding_event = holding_release_map.get(release_id)
        if net_quantity.copy_abs() <= SHARE_TOLERANCE:
            if holding_event is not None:
                problems.append(f"mismatch: zero-net release {release_id} appears in holdings Activity")
        elif holding_event is None:
            problems.append(f"missing: holdings Activity release {release_id}")
        elif (
            holding_event.get("date") != detail.get("settlement_date")
            or abs(parse_decimal(holding_event.get("quantity", "0")) - net_quantity) > SHARE_TOLERANCE
        ):
            problems.append(f"mismatch: holdings Activity release {release_id}")

    extra_rsu_ids = sorted(set(rsu_release_map) - set(detail_release_map))
    extra_holding_ids = sorted(set(holding_release_map) - set(detail_release_map))
    if extra_rsu_ids:
        problems.append(f"missing: detail sections for RSU Activity releases {extra_rsu_ids}")
    if extra_holding_ids:
        problems.append(f"missing: detail sections for holdings Activity releases {extra_holding_ids}")

    activity_sales_by_date: dict[str, list[Decimal]] = defaultdict(list)
    detail_sales_by_date: dict[str, list[Decimal]] = defaultdict(list)
    for event in holding_sales:
        activity_sales_by_date[event.get("date", "")].append(
            parse_decimal(event.get("quantity", "0"))
        )
    for detail in detail_withdrawals:
        if detail.get("settlement_date") and detail.get("quantity_sold"):
            detail_sales_by_date[detail["settlement_date"]].append(
                parse_decimal(detail["quantity_sold"])
            )
    all_sale_dates = sorted(set(activity_sales_by_date) | set(detail_sales_by_date))
    for sale_date in all_sale_dates:
        activity_quantities = sorted(activity_sales_by_date[sale_date])
        detail_quantities = sorted(detail_sales_by_date[sale_date])
        if len(activity_quantities) != len(detail_quantities) or any(
            abs(activity_quantity - detail_quantity) > SHARE_TOLERANCE
            for activity_quantity, detail_quantity in zip(activity_quantities, detail_quantities)
        ):
            problems.append(f"mismatch: holdings Activity sales on {sale_date or 'unknown date'}")

    opening_text = metadata.get("opening_shares", "")
    closing_text = metadata.get("closing_shares", "")
    activity_calculated_closing: Decimal | None = None
    activity_difference: Decimal | None = None
    if opening_text and closing_text:
        activity_calculated_closing = parse_decimal(opening_text)
        activity_calculated_closing += sum(
            (parse_decimal(event.get("quantity", "0")) for event in holding_releases),
            Decimal("0"),
        )
        activity_calculated_closing -= sum(
            (parse_decimal(event.get("quantity", "0")) for event in holding_sales),
            Decimal("0"),
        )
        activity_difference = activity_calculated_closing - parse_decimal(closing_text)
        if abs(activity_difference) > SHARE_TOLERANCE:
            problems.append("mismatch: holdings Activity movements do not reach reported closing")

    status = "; ".join(problems) if problems else "ok"
    notes = (
        f"holdings releases={len(holding_releases)}; holdings sales={len(holding_sales)}; "
        f"RSU releases={len(rsu_releases)}; detail releases={len(detail_releases)}; "
        f"detail withdrawals={len(detail_withdrawals)}"
    )
    return {
        "source_type": "Activity reconciliation",
        "id": "",
        "status": status,
        "transaction_date": metadata.get("closing_date", ""),
        "date_source": "Independent Activity ledgers",
        "settlement_date": "",
        "fx_lookup_date_for_spreadsheet": "",
        "quantity_released": "",
        "quantity_sold": "",
        "usd_price": "",
        "usd_fees": "",
        "brokerage_commission": "",
        "supplemental_transaction_fee": "",
        "wire_fee_excluded": "",
        "gross_value_or_proceeds": "",
        "opening_shares": opening_text,
        "closing_shares": closing_text,
        "calculated_closing_shares": fmt_decimal(activity_calculated_closing),
        "minimum_running_shares": "",
        "balance_difference": fmt_decimal(activity_difference),
        "notes": notes,
    }


def audit_status_is_failure(status: str) -> bool:
    """Treat explicit checks as warnings while failing all other non-ok states."""
    parts = [part.strip() for part in status.split(";") if part.strip()]
    return not parts or any(part != "ok" and not part.startswith("check:") for part in parts)


def build_run_audit(source_type: str, status: str, notes: str) -> dict[str, str]:
    return {
        "source_type": source_type,
        "id": "",
        "status": status,
        "notes": notes,
    }


def clear_enriched_values(rows: list[dict[str, str]]) -> None:
    """Remove partially populated values that could be mistaken for CGT-ready output."""
    for row in rows:
        for field_name in ["FX Rate", "GBP Price", "GBP Fees", "CGT Calculator String"]:
            row[field_name] = ""
        row.pop("_fx_rate_date", None)
        row.pop("_fx_source", None)


def as_public_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{key: row.get(key, "") for key in PUBLIC_FIELDNAMES} for row in rows]


def stage_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> Path:
    """Write and sync a CSV to a temporary file beside its final destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary_path
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def write_csv_atomic(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    temporary_path = stage_csv(path, fieldnames, rows)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def publish_artifacts(
    rows_path: Path,
    rows: list[dict[str, str]],
    audit_path: Path,
    audit: list[dict[str, str]],
) -> None:
    """Publish audit first and actionable rows last, after both CSVs stage successfully."""
    audit_temporary_path: Path | None = None
    rows_temporary_path: Path | None = None
    try:
        audit_temporary_path = stage_csv(audit_path, AUDIT_FIELDNAMES, audit)
        rows_temporary_path = stage_csv(rows_path, PUBLIC_FIELDNAMES, rows)
        os.replace(audit_temporary_path, audit_path)
        audit_temporary_path = None
        os.replace(rows_temporary_path, rows_path)
        rows_temporary_path = None
    finally:
        if audit_temporary_path is not None:
            audit_temporary_path.unlink(missing_ok=True)
        if rows_temporary_path is not None:
            rows_temporary_path.unlink(missing_ok=True)


def enrich_audit_with_fx(audit: list[dict[str, str]], rows: list[dict[str, str]]) -> None:
    by_id_type = {(row["ID"], row["Type"]): row for row in rows}
    for entry in audit:
        if entry["source_type"] == "Release section":
            release = by_id_type.get((entry["id"], "Release"))
            sell_to_cover = by_id_type.get((entry["id"], "Sell to Cover"))
            if release and sell_to_cover and release.get("_fx_rate_date") and sell_to_cover.get("_fx_rate_date"):
                entry["fx_rate_for_spreadsheet"] = (
                    f"Release row: {release['FX Rate']} ({release['_fx_rate_date']}); "
                    f"Sell to Cover row: {sell_to_cover['FX Rate']} ({sell_to_cover['_fx_rate_date']})"
                )
                entry["fx_rate_source"] = "Frankfurter USD->GBP"
            else:
                entry["fx_rate_for_spreadsheet"] = ""
                entry["fx_rate_source"] = ""
        elif entry["source_type"] == "Withdrawal section":
            withdrawal = by_id_type.get((entry["id"], "Withdrawal"))
            if withdrawal and withdrawal.get("_fx_rate_date"):
                entry["fx_rate_for_spreadsheet"] = f"{withdrawal['FX Rate']} ({withdrawal['_fx_rate_date']})"
                entry["fx_rate_source"] = "Frankfurter USD->GBP"
            else:
                entry["fx_rate_for_spreadsheet"] = ""
                entry["fx_rate_source"] = ""
        else:
            entry["fx_rate_for_spreadsheet"] = ""
            entry["fx_rate_source"] = ""


def parse_releases(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    sections = split_sections(
        text,
        r"Share Units - Release \(",
        TRANSACTION_HEADING_RE,
    )
    for source_order, section in enumerate(sections):
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
            "gross_release_value": gross_release_value,
        }
        missing = [key for key, value in required.items() if value is None]
        status = "ok" if not missing else f"missing: {', '.join(missing)}"
        if not missing:
            monetary_problem = gross_value_problem(
                quantity_released,
                release_price,
                gross_release_value,
                "gross release value",
            )
            if monetary_problem:
                status = monetary_problem

        if status != "ok":
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
                "_fx_lookup_date": parse_short_date(release_date),
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
                "fx_lookup_date_for_spreadsheet": (
                    f"Release and Sell to Cover rows: {parse_short_date(release_date)}"
                ),
                "quantity_released": quantity_released,
                "quantity_sold": quantity_sold,
                "usd_price": release_price,
                "usd_fees": fmt_money(fee_total),
                "brokerage_commission": fee_components["Brokerage Commission"],
                "supplemental_transaction_fee": fee_components["Supplemental Transaction Fee"],
                "wire_fee_excluded": "",
                "gross_value_or_proceeds": gross_release_value or "",
                "notes": (
                    "Release Date is the acquisition/disposal and FX lookup date for both rows; "
                    "Settlement Date is retained only for statement reconciliation. Gross release "
                    "value reconciles to quantity released x release price within $0.01."
                ),
            }
        )
    return rows, audit


def parse_withdrawals(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    sections = split_sections(text, r"Withdrawal on ", TRANSACTION_HEADING_RE)
    for source_order, section in enumerate(sections):
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
            "gross_proceeds": gross_proceeds,
        }
        missing = [key for key, value in required.items() if value is None]
        status = "ok" if not missing else f"missing: {', '.join(missing)}"
        if not missing:
            monetary_problem = gross_value_problem(
                shares_sold,
                usd_price,
                gross_proceeds,
                "gross proceeds",
            )
            if monetary_problem:
                status = monetary_problem

        withdrawal_date = parse_long_date(withdrawal_date_long) if withdrawal_date_long else ""
        settlement_date_iso = parse_short_date(settlement_date) if settlement_date else ""
        if (
            status == "ok"
            and withdrawal_date
            and settlement_date_iso
            and withdrawal_date == settlement_date_iso
        ):
            status = "check: withdrawal date equals settlement date"
        if status != "ok" and not status.startswith("check:"):
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
                "notes": (
                    "Date and FX lookup date come from the Withdrawal on ... heading, not Settlement "
                    "Date. Gross proceeds reconcile to shares sold x market price within $0.01. "
                    "Wire/EFT fees excluded. Brokerage commission and supplemental transaction "
                    "fee included."
                ),
            }
        )
    return rows, audit


def parse_html_releases(tables: list[list[list[str]]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    source_order = 0
    for index, table in enumerate(tables):
        title = table_title(table)
        if not title.startswith("Share Units - Release ("):
            continue

        fields = field_pairs(table)
        value_of_shares_sold = two_column_pairs(tables[index + 1]) if index + 1 < len(tables) else {}
        release_id = first_match(r"Share Units - Release \(([^)]+)\)", title)
        settlement_date = fields.get("Settlement Date")
        release_date = fields.get("Release Date")
        quantity_released = fields.get("Quantity Released")
        release_price = money_amount(fields.get("Release Price"))
        quantity_sold = fields.get("Number of Restricted Awards Sold")
        sale_price = money_amount(fields.get("Sale Price"))
        gross_release_value = money_amount(fields.get("Gross Release Value"))
        brokerage_commission = fmt_abs_money(value_of_shares_sold.get("Brokerage Commission"))
        supplemental_transaction_fee = fmt_abs_money(value_of_shares_sold.get("Supplemental Transaction Fee"))
        fee_total = sum(
            (parse_decimal(amount) for amount in [brokerage_commission, supplemental_transaction_fee] if amount),
            Decimal("0"),
        )
        fee_components = {
            "Brokerage Commission": brokerage_commission,
            "Supplemental Transaction Fee": supplemental_transaction_fee,
        }

        required = {
            "release_id": release_id,
            "settlement_date": settlement_date,
            "release_date": release_date,
            "quantity_released": quantity_released,
            "release_price": release_price,
            "quantity_sold": quantity_sold,
            "sale_price": sale_price,
            "gross_release_value": gross_release_value,
        }
        missing = [key for key, value in required.items() if value is None]
        status = "ok" if not missing else f"missing: {', '.join(missing)}"
        if not missing:
            monetary_problem = gross_value_problem(
                quantity_released,
                release_price,
                gross_release_value,
                "gross release value",
            )
            if monetary_problem:
                status = monetary_problem

        release_date_iso = parse_short_date(release_date) if release_date else ""
        settlement_date_iso = parse_short_date(settlement_date) if settlement_date else ""
        if status != "ok":
            audit.append(
                {
                    "source_type": "Release section",
                    "id": release_id or "",
                    "status": status,
                    "transaction_date": release_date_iso,
                    "date_source": "Release Date",
                    "settlement_date": settlement_date_iso,
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
            source_order += 1
            continue

        rows.append(
            {
                "ID": release_id,
                "Date": release_date_iso,
                "Type": "Release",
                "Quantity": quantity_released,
                "USD Price": release_price,
                "USD Fees": "",
                "FX Rate": "",
                "_source_order": source_order,
                "_sub_order": 0,
                "_fx_lookup_date": release_date_iso,
            }
        )
        rows.append(
            {
                "ID": release_id,
                "Date": release_date_iso,
                "Type": "Sell to Cover",
                "Quantity": quantity_sold,
                "USD Price": sale_price,
                "USD Fees": fmt_money(fee_total),
                "FX Rate": "",
                "_source_order": source_order,
                "_sub_order": 1,
                "_fx_lookup_date": release_date_iso,
            }
        )
        audit.append(
            {
                "source_type": "Release section",
                "id": release_id,
                "status": status,
                "transaction_date": release_date_iso,
                "date_source": "Release Date",
                "settlement_date": settlement_date_iso,
                "fx_lookup_date_for_spreadsheet": (
                    f"Release and Sell to Cover rows: {release_date_iso}"
                ),
                "quantity_released": quantity_released,
                "quantity_sold": quantity_sold,
                "usd_price": release_price,
                "usd_fees": fmt_money(fee_total),
                "brokerage_commission": fee_components["Brokerage Commission"],
                "supplemental_transaction_fee": fee_components["Supplemental Transaction Fee"],
                "wire_fee_excluded": "",
                "gross_value_or_proceeds": gross_release_value or "",
                "notes": (
                    "Release Date is the acquisition/disposal and FX lookup date for both rows; "
                    "Settlement Date is retained only for statement reconciliation. Gross release "
                    "value reconciles to quantity released x release price within $0.01."
                ),
            }
        )
        source_order += 1
    return rows, audit


def parse_html_withdrawals(tables: list[list[list[str]]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    source_order = 0
    for index, table in enumerate(tables):
        title = table_title(table)
        if not title.startswith("Withdrawal on "):
            continue

        fields = field_pairs(table)
        sale_breakdown = two_column_pairs(tables[index + 1]) if index + 1 < len(tables) else {}
        withdrawal_date_long = title.removeprefix("Withdrawal on ")
        reference_number = fields.get("Reference Number")
        settlement_date = fields.get("Settlement Date")
        usd_price = money_amount(fields.get("Market Price Per Unit"))
        shares_sold = fields.get("Shares Sold")
        gross_proceeds = money_amount(sale_breakdown.get("Gross Proceeds"))
        wire_fee = money_amount(sale_breakdown.get("Wire Fee"))
        brokerage_commission = fmt_abs_money(sale_breakdown.get("Brokerage Commission"))
        supplemental_transaction_fee = fmt_abs_money(sale_breakdown.get("Supplemental Transaction Fee"))
        fee_total = sum(
            (parse_decimal(amount) for amount in [brokerage_commission, supplemental_transaction_fee] if amount),
            Decimal("0"),
        )
        fee_components = {
            "Brokerage Commission": brokerage_commission,
            "Supplemental Transaction Fee": supplemental_transaction_fee,
        }

        required = {
            "withdrawal_date": withdrawal_date_long,
            "reference_number": reference_number,
            "settlement_date": settlement_date,
            "usd_price": usd_price,
            "shares_sold": shares_sold,
            "gross_proceeds": gross_proceeds,
        }
        missing = [key for key, value in required.items() if value is None]
        status = "ok" if not missing else f"missing: {', '.join(missing)}"
        if not missing:
            monetary_problem = gross_value_problem(
                shares_sold,
                usd_price,
                gross_proceeds,
                "gross proceeds",
            )
            if monetary_problem:
                status = monetary_problem

        withdrawal_date = parse_long_date(withdrawal_date_long) if withdrawal_date_long else ""
        settlement_date_iso = parse_short_date(settlement_date) if settlement_date else ""
        if (
            status == "ok"
            and withdrawal_date
            and settlement_date_iso
            and withdrawal_date == settlement_date_iso
        ):
            status = "check: withdrawal date equals settlement date"
        if status != "ok" and not status.startswith("check:"):
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
            source_order += 1
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
                "notes": (
                    "Date and FX lookup date come from the Withdrawal on ... heading, not Settlement "
                    "Date. Gross proceeds reconcile to shares sold x market price within $0.01. "
                    "Wire/EFT fees excluded. Brokerage commission and supplemental transaction "
                    "fee included."
                ),
            }
        )
        source_order += 1
    return rows, audit


def sort_key(row: dict[str, str]) -> tuple[str, int, int, str]:
    same_day_group = 1 if row["Type"] == "Withdrawal" else 0
    return (row["Date"], same_day_group, int(row["_source_order"]), row["ID"])


def is_html_report(report_path: Path) -> bool:
    if report_path.suffix.lower() in {".html", ".htm"}:
        return True
    sample = report_path.read_bytes()[:2048].lower()
    return b"<html" in sample or b"<!doctype html" in sample


def parse_pdf_report(report_path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    reader = PdfReader(str(report_path))
    text = clean_text("\n\n".join((page.extract_text() or "") for page in reader.pages))
    TEXT_PATH.write_text(text, encoding="utf-8")

    release_rows, release_audit = parse_releases(text)
    withdrawal_rows, withdrawal_audit = parse_withdrawals(text)
    metadata = {
        "input_format": "pdf",
        "pages": str(len(reader.pages)),
        **parse_pdf_fund_metadata(text),
        **parse_pdf_holding_summary(text),
        **parse_pdf_holding_activity(text),
        **parse_pdf_rsu_activity(text),
    }
    audit = release_audit + withdrawal_audit
    audit.append(build_activity_reconciliation(metadata, audit))
    return release_rows + withdrawal_rows, audit, metadata


def parse_html_report(report_path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    parser = StatementTableParser()
    parser.feed(report_path.read_text(encoding="utf-8", errors="replace"))
    TEXT_PATH.write_text("\n\n".join(table_text(table) for table in parser.tables), encoding="utf-8")

    release_rows, release_audit = parse_html_releases(parser.tables)
    withdrawal_rows, withdrawal_audit = parse_html_withdrawals(parser.tables)
    metadata = {
        "input_format": "html",
        "tables": str(len(parser.tables)),
        **parse_html_fund_metadata(parser.tables),
        **parse_html_holding_summary(parser.tables),
        **parse_html_holding_activity(parser.tables),
        **parse_html_rsu_activity(parser.tables),
    }
    audit = release_audit + withdrawal_audit
    audit.append(build_activity_reconciliation(metadata, audit))
    return release_rows + withdrawal_rows, audit, metadata


def parse_report(report_path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    if is_html_report(report_path):
        return parse_html_report(report_path)
    return parse_pdf_report(report_path)


def main() -> int:
    global OUT_DIR, TEXT_PATH, FX_CACHE_PATH

    args = parse_args()
    report_path = args.report
    OUT_DIR = args.outputs_dir
    TEXT_PATH = args.work_dir / "statement_raw_text.txt"
    FX_CACHE_PATH = args.work_dir / "frankfurter_usd_gbp_rates.json"

    csv_path = args.out or OUT_DIR / "shareworks_extracted_rows.csv"
    audit_path = OUT_DIR / "shareworks_extraction_audit.csv"
    artifact_paths = {csv_path.resolve(), audit_path.resolve()}
    protected_paths = {
        report_path.resolve(),
        TEXT_PATH.resolve(),
        FX_CACHE_PATH.resolve(),
    }
    if len(artifact_paths) != 2 or artifact_paths & protected_paths:
        print("FAILED output paths must be distinct from each other, the input, and work files")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    pending_audit = build_run_audit(
        "Extraction run",
        "incomplete: extraction did not finish",
        "This placeholder is atomically replaced only after final audit and rows are ready.",
    )
    write_csv_atomic(csv_path, PUBLIC_FIELDNAMES, [])
    write_csv_atomic(audit_path, AUDIT_FIELDNAMES, [pending_audit])

    parsed_rows, audit, metadata = parse_report(report_path)
    rows = sorted(parsed_rows, key=sort_key)
    audit.append(build_security_validation(metadata, args.cgt_symbol))
    opening_shares = parse_decimal(metadata["opening_shares"]) if metadata.get("opening_shares") else Decimal("0")
    calculated_closing_shares, minimum_running_shares = calculate_share_balances(rows, opening_shares)
    audit.append(
        build_holding_reconciliation(
            metadata,
            calculated_closing_shares,
            minimum_running_shares,
        )
    )
    apply_running_totals(rows, opening_shares)
    preflight_failed = [row for row in audit if audit_status_is_failure(row["status"])]
    clear_enriched_values(rows)
    if not preflight_failed:
        try:
            apply_fx_rates(rows)
            apply_derived_values(rows, opening_shares, symbol=args.cgt_symbol)
        except Exception as error:
            clear_enriched_values(rows)
            apply_running_totals(rows, opening_shares)
            error_summary = clean_cell_text(f"{type(error).__name__}: {error}")
            audit.append(
                build_run_audit(
                    "FX enrichment",
                    f"failed: {error_summary}",
                    "No FX, GBP, or CGT Calculator values were published for this run.",
                )
            )
    enrich_audit_with_fx(audit, rows)
    public_rows = as_public_rows(rows)
    publish_artifacts(csv_path, public_rows, audit_path, audit)

    print(f"input_format={metadata['input_format']}")
    if "pages" in metadata:
        print(f"pages={metadata['pages']}")
    if "tables" in metadata:
        print(f"tables={metadata['tables']}")
    print(f"release_sections={len([row for row in audit if row['source_type'] == 'Release section'])}")
    print(f"withdrawal_sections={len([row for row in audit if row['source_type'] == 'Withdrawal section'])}")
    print(f"spreadsheet_rows={len(rows)}")
    print(f"fx_rates={len({row['_fx_rate_date'] for row in rows if row.get('_fx_rate_date')})}")
    print(f"rows_csv={csv_path}")
    print(f"audit_csv={audit_path}")
    warnings = [row for row in audit if row["status"].startswith("check:")]
    failed = [row for row in audit if audit_status_is_failure(row["status"])]
    print(f"warning_sections={len(warnings)}")
    print(f"failed_sections={len(failed)}")
    print(f"opening_shares={metadata.get('opening_shares', '')}")
    print(f"reported_closing_shares={metadata.get('closing_shares', '')}")
    print(f"calculated_closing_shares={fmt_decimal(calculated_closing_shares)}")
    print(f"cgt_ready={'false' if failed else 'true'}")
    for row in warnings:
        print(f"WARNING {row['source_type']} {row['id']}: {row['status']}")
    if failed:
        for row in failed:
            print(f"FAILED {row['source_type']} {row['id']}: {row['status']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
