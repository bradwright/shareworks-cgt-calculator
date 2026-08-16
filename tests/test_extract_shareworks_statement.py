import argparse
import copy
import csv
import io
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from src import extract_shareworks_statement as extractor


PDF_HOLDING_TEXT = """
Starting balance 41
$531.89 USD$555.59 USD$74.81 USD7.109823$0.00 USD Opening Value05-Apr-2024
$1,359.35 USD$1,668.28 USD$76.89 USD17.679174$0.00 USD Closing Value04-Apr-2025
"""

PDF_ACTIVITY_TEXT = """
Share Units (RSU) - Activity
VestedOutstandingGrant NameActivityDate
-2.000000 -2.000000-2.000000 -2.0000002024-1-QEG-RSURelease
(REL1)
01-Jan-2024
0.000000 Ending balance04-Jan-2024

Activity
Market ValueBook ValueShare PriceNumber of SharesCashType of MoneyActivityEntry Date
Fund: Example
$0.00 USD$0.00 USD$10.00 USD0$0.00 USD Opening Value01-Jan-2024
$15.00 USD 1.500000 EmployeeRelease (REL1)02-Jan-2024
$-15.00 USD$10.00 USD-1.500000 EmployeeSale03-Jan-2024
$0.00 USD$0.00 USD$10.00 USD0$0.00 USD Closing Value04-Jan-2024
"""

PDF_RELEASE_TEXT = """
Share Units - Release (REL1)
02-May-2024 Settlement Date:
30-Apr-2024 Release Date:
2 Quantity Released:
$10.00 USD Release Price:
1 Number of Restricted Awards Sold:
$10.00 USD Sale Price:
$20.00 USD Gross Release Value:
"""

PDF_WITHDRAWAL_TEXT = """
Withdrawal on May 10, 2024
SALE1 Reference Number:
13-May-2024 Settlement Date:
$11.00 USD Market Price Per Unit:
1 Shares Sold:
$11.00 USD Gross Proceeds
$-9.00 USD Brokerage Commission
"""


def metadata(opening: str, closing: str) -> dict[str, str]:
    return {
        "input_format": "html",
        "tables": "1",
        "opening_snapshot_count": "1",
        "closing_snapshot_count": "1",
        "opening_shares": opening,
        "opening_date": "2024-04-05",
        "closing_shares": closing,
        "closing_date": "2025-04-04",
        "fund_count": "1",
        "fund_names": '["Example Corporation Common Stock"]',
    }


def row(row_type: str, quantity: str, row_id: str = "TEST") -> dict[str, str]:
    return {
        "ID": row_id,
        "Date": "2024-04-30",
        "Type": row_type,
        "Quantity": quantity,
        "USD Price": "10",
        "USD Fees": "",
        "FX Rate": "",
        "_source_order": 0,
        "_sub_order": 0,
        "_fx_lookup_date": "2024-04-30",
    }


def release_audit(
    release_id: str = "REL1",
    gross: str = "2",
    sold: str = "0.5",
) -> dict[str, str]:
    return {
        "source_type": "Release section",
        "id": release_id,
        "status": "ok",
        "transaction_date": "2024-01-01",
        "settlement_date": "2024-01-02",
        "quantity_released": gross,
        "quantity_sold": sold,
    }


def withdrawal_audit(quantity: str = "1.5") -> dict[str, str]:
    return {
        "source_type": "Withdrawal section",
        "id": "SALE1",
        "status": "ok",
        "transaction_date": "2024-01-02",
        "settlement_date": "2024-01-03",
        "quantity_sold": quantity,
    }


class HoldingParserTests(unittest.TestCase):
    def test_parses_exact_pypdf_activity_rows(self) -> None:
        result = extractor.parse_pdf_holding_summary(PDF_HOLDING_TEXT)

        self.assertEqual(result["opening_snapshot_count"], "1")
        self.assertEqual(result["opening_shares"], "7.109823")
        self.assertEqual(result["opening_date"], "2024-04-05")
        self.assertEqual(result["opening_book_value_usd"], "555.59")
        self.assertEqual(result["closing_shares"], "17.679174")
        self.assertEqual(result["closing_date"], "2025-04-04")

    def test_duplicate_pdf_snapshots_are_ambiguous(self) -> None:
        result = extractor.parse_pdf_holding_summary(
            PDF_HOLDING_TEXT
            + "$1.00 USD$1.00 USD$1.00 USD1$0.00 USD Opening Value06-Apr-2024\n"
        )

        self.assertEqual(result["opening_snapshot_count"], "2")
        self.assertNotIn("opening_shares", result)

    def test_same_line_duplicate_pdf_snapshots_are_ambiguous(self) -> None:
        duplicate = (
            "$1.00 USD$1.00 USD$1.00 USD1$0.00 USD Opening Value06-Apr-2024"
        )
        result = extractor.parse_pdf_holding_summary(
            PDF_HOLDING_TEXT.replace("Opening Value05-Apr-2024", f"Opening Value05-Apr-2024{duplicate}")
        )

        self.assertEqual(result["opening_snapshot_count"], "2")
        self.assertNotIn("opening_shares", result)

    def test_html_parser_uses_headers_not_fixed_positions(self) -> None:
        html = """
        <html><body><table>
          <tr><th>Activity</th><th>Entry Date</th><th>Share Price</th>
              <th>Number of Shares</th><th>Book Value</th><th>Market Value</th></tr>
          <tr><td>Opening Value</td><td>05-Apr-2024</td><td>$74.81 USD</td>
              <td>7.109823</td><td>$555.59 USD</td><td>$531.89 USD</td></tr>
          <tr><td>Closing Value</td><td>04-Apr-2025</td><td>$76.89 USD</td>
              <td>17.679174</td><td>$1,668.28 USD</td><td>$1,359.35 USD</td></tr>
        </table></body></html>
        """
        parser = extractor.StatementTableParser()
        parser.feed(html)

        result = extractor.parse_html_holding_summary(parser.tables)

        self.assertEqual(result["opening_shares"], "7.109823")
        self.assertEqual(result["opening_book_value_usd"], "555.59")
        self.assertEqual(result["closing_shares"], "17.679174")

    def test_duplicate_html_snapshots_are_ambiguous(self) -> None:
        tables = [[
            ["Number of Shares", "Activity", "Entry Date"],
            ["1", "Opening Value", "01-Jan-2024"],
            ["2", "Opening Value", "02-Jan-2024"],
            ["3", "Closing Value", "03-Jan-2024"],
        ]]

        result = extractor.parse_html_holding_summary(tables)

        self.assertEqual(result["opening_snapshot_count"], "2")
        self.assertNotIn("opening_shares", result)

    def test_malformed_duplicate_html_snapshot_is_still_ambiguous(self) -> None:
        tables = [[
            ["Number of Shares", "Activity", "Entry Date"],
            ["1", "Opening Value", "01-Jan-2024"],
            ["not-a-number", "Opening Value", "02-Jan-2024"],
            ["3", "Closing Value", "03-Jan-2024"],
        ]]

        result = extractor.parse_html_holding_summary(tables)

        self.assertEqual(result["opening_snapshot_count"], "2")
        self.assertNotIn("opening_shares", result)

    def test_html_parser_accepts_rows_without_optional_trailing_values(self) -> None:
        tables = [[
            ["Number of Shares", "Activity", "Entry Date", "Market Value"],
            ["0", "Opening Value", "01-Jan-2024"],
            ["2", "Closing Value", "31-Dec-2024"],
        ]]

        result = extractor.parse_html_holding_summary(tables)

        self.assertEqual(result["opening_shares"], "0")
        self.assertEqual(result["closing_shares"], "2")


class SecurityValidationTests(unittest.TestCase):
    def test_pdf_and_html_fund_parsers_deduplicate_fund(self) -> None:
        pdf_result = extractor.parse_pdf_fund_metadata(
            "Fund: Example Corporation Common Stock\n"
            "Fund: Example Corporation Common Stock\n"
        )
        html_result = extractor.parse_html_fund_metadata(
            [
                [["Fund: Example Corporation Common Stock"]],
                [["Fund", "Example Corporation Common Stock"]],
            ]
        )

        self.assertEqual(pdf_result["fund_count"], "1")
        self.assertEqual(html_result["fund_count"], "1")
        self.assertEqual(
            extractor.build_security_validation(pdf_result, "EXMPL")["status"],
            "ok",
        )
        self.assertEqual(
            extractor.build_security_validation(html_result, "EXMPL")["status"],
            "ok",
        )

    def test_missing_invalid_symbol_and_multiple_funds_are_blocking(self) -> None:
        missing = extractor.build_security_validation({}, "EXMPL")
        invalid_symbol = extractor.build_security_validation(
            {"fund_names": '["Example Corporation Common Stock"]'},
            "not valid",
        )
        multiple = extractor.build_security_validation(
            {
                "fund_names": (
                    '["Example Corporation Common Stock", "Second Corporation"]'
                )
            },
            "EXMPL",
        )

        self.assertIn("missing", missing["status"])
        self.assertIn("invalid", invalid_symbol["status"])
        self.assertIn("ambiguous", multiple["status"])
        for result in [missing, invalid_symbol, multiple]:
            self.assertTrue(extractor.audit_status_is_failure(result["status"]))


class TransactionParserTests(unittest.TestCase):
    @staticmethod
    def html_release_tables(
        gross_release_value: str = "$20.00 USD",
    ) -> list[list[list[str]]]:
        return [
            [
                ["Share Units - Release (REL1)"],
                ["Settlement Date", "02-May-2024"],
                ["Release Date", "30-Apr-2024"],
                ["Quantity Released", "2"],
                ["Release Price", "$10.00 USD"],
                ["Number of Restricted Awards Sold", "1"],
                ["Sale Price", "$10.00 USD"],
                ["Gross Release Value", gross_release_value],
            ],
            [
                ["Value of Shares Sold"],
                ["Brokerage Commission", "$1.00 USD"],
            ],
        ]

    def test_pdf_release_uses_disposal_date_and_own_fees(self) -> None:
        rows, audit = extractor.parse_releases(PDF_RELEASE_TEXT + PDF_WITHDRAWAL_TEXT)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["_fx_lookup_date"], "2024-04-30")
        self.assertEqual(rows[1]["Date"], "2024-04-30")
        self.assertEqual(rows[1]["_fx_lookup_date"], "2024-04-30")
        self.assertEqual(rows[1]["USD Fees"], "0.00")
        self.assertEqual(audit[0]["brokerage_commission"], "")
        self.assertEqual(audit[0]["status"], "ok")

    def test_html_release_uses_disposal_date_for_fx(self) -> None:
        rows, audit = extractor.parse_html_releases(self.html_release_tables())

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["_fx_lookup_date"], "2024-04-30")
        self.assertEqual(rows[1]["_fx_lookup_date"], "2024-04-30")
        self.assertEqual(audit[0]["status"], "ok")

    def test_impossible_pdf_and_html_release_values_are_blocking(self) -> None:
        pdf_rows, pdf_audit = extractor.parse_releases(
            PDF_RELEASE_TEXT.replace(
                "$20.00 USD Gross Release Value",
                "$999.00 USD Gross Release Value",
            )
        )
        html_rows, html_audit = extractor.parse_html_releases(
            self.html_release_tables("$999.00 USD")
        )

        self.assertEqual(pdf_rows, [])
        self.assertEqual(html_rows, [])
        self.assertIn("mismatch: gross release value", pdf_audit[0]["status"])
        self.assertIn("mismatch: gross release value", html_audit[0]["status"])

    def test_impossible_pdf_and_html_withdrawal_values_are_blocking(self) -> None:
        pdf_rows, pdf_audit = extractor.parse_withdrawals(
            PDF_WITHDRAWAL_TEXT.replace(
                "$11.00 USD Gross Proceeds",
                "$999.00 USD Gross Proceeds",
            )
        )
        html_tables = [
            [
                ["Withdrawal on May 10, 2024"],
                ["Reference Number", "SALE1"],
                ["Settlement Date", "13-May-2024"],
                ["Market Price Per Unit", "$11.00 USD"],
                ["Shares Sold", "1"],
            ],
            [["Sale Breakdown"], ["Gross Proceeds", "$999.00 USD"]],
        ]
        html_rows, html_audit = extractor.parse_html_withdrawals(html_tables)

        self.assertEqual(pdf_rows, [])
        self.assertEqual(html_rows, [])
        self.assertIn("mismatch: gross proceeds", pdf_audit[0]["status"])
        self.assertIn("mismatch: gross proceeds", html_audit[0]["status"])


class ActivityReconciliationTests(unittest.TestCase):
    def activity_metadata(self, text: str = PDF_ACTIVITY_TEXT) -> dict[str, str]:
        return {
            **extractor.parse_pdf_holding_summary(text),
            **extractor.parse_pdf_holding_activity(text),
            **extractor.parse_pdf_rsu_activity(text),
        }

    def test_pdf_activity_ledgers_parse_and_match_details(self) -> None:
        report_metadata = self.activity_metadata()

        result = extractor.build_activity_reconciliation(
            report_metadata,
            [release_audit(), withdrawal_audit()],
        )

        self.assertEqual(report_metadata["holding_activity_candidate_count"], "2")
        self.assertEqual(report_metadata["rsu_activity_candidate_count"], "1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["calculated_closing_shares"], "0")

    def test_offsetting_activity_still_requires_detail_sections(self) -> None:
        result = extractor.build_activity_reconciliation(self.activity_metadata(), [])

        self.assertEqual(result["balance_difference"], "0")
        self.assertIn("detail sections", result["status"])
        self.assertIn("holdings Activity sales", result["status"])

    def test_rsu_ledger_requires_zero_net_release_detail(self) -> None:
        text = PDF_ACTIVITY_TEXT.replace(
            "$15.00 USD 1.500000 EmployeeRelease (REL1)02-Jan-2024\n"
            "$-15.00 USD$10.00 USD-1.500000 EmployeeSale03-Jan-2024\n",
            "",
        )
        report_metadata = self.activity_metadata(text)
        complete = extractor.build_activity_reconciliation(
            report_metadata,
            [release_audit(gross="2", sold="2")],
        )
        missing = extractor.build_activity_reconciliation(report_metadata, [])

        self.assertEqual(complete["status"], "ok")
        self.assertIn("detail sections for RSU Activity releases", missing["status"])

    def test_html_unknown_holding_activity_is_blocking(self) -> None:
        tables = [
            [
                ["Number of Shares", "Activity", "Entry Date"],
                ["0", "Opening Value", "01-Jan-2024"],
                ["1", "Transfer", "02-Jan-2024"],
                ["0", "Closing Value", "04-Jan-2024"],
            ],
            [
                ["Quantity", "Grant Name", "Activity", "Date"],
                ["-2", "2024-1-QEG-RSU", "Release (REL1)", "01-Jan-2024"],
            ],
        ]

        holding_result = extractor.parse_html_holding_activity(tables)
        rsu_result = extractor.parse_html_rsu_activity(tables)

        self.assertIn("unsupported", holding_result["holding_activity_status"])
        self.assertEqual(rsu_result["rsu_activity_status"], "ok")
        self.assertEqual(rsu_result["rsu_activity_candidate_count"], "1")

    def test_html_activity_ledgers_match_details_by_headers(self) -> None:
        tables = [
            [
                ["Entry Date", "Activity", "Number of Shares"],
                ["01-Jan-2024", "Opening Value", "0"],
                ["02-Jan-2024", "Release (REL1)", "1.5"],
                ["03-Jan-2024", "Sale", "-1.5"],
                ["04-Jan-2024", "Closing Value", "0"],
            ],
            [
                ["Date", "Activity", "Grant Name", "Quantity"],
                ["01-Jan-2024", "Release (REL1)", "2024-1-QEG-RSU", "-2"],
            ],
        ]
        report_metadata = {
            **extractor.parse_html_holding_summary(tables),
            **extractor.parse_html_holding_activity(tables),
            **extractor.parse_html_rsu_activity(tables),
        }

        result = extractor.build_activity_reconciliation(
            report_metadata,
            [release_audit(), withdrawal_audit()],
        )

        self.assertEqual(result["status"], "ok")

    def test_html_blank_activity_cells_with_data_are_blocking(self) -> None:
        tables = [
            [
                ["Number of Shares", "Activity", "Entry Date"],
                ["0", "Opening Value", "01-Jan-2024"],
                ["1", "", "02-Jan-2024"],
                ["-1", "", "03-Jan-2024"],
                ["0", "Closing Value", "04-Jan-2024"],
            ],
            [
                ["Quantity", "Grant Name", "Activity", "Date"],
                ["-2", "2024-1-QEG-RSU", "", "01-Jan-2024"],
            ],
        ]

        holding_result = extractor.parse_html_holding_activity(tables)
        rsu_result = extractor.parse_html_rsu_activity(tables)

        self.assertEqual(holding_result["holding_activity_candidate_count"], "2")
        self.assertIn("unsupported", holding_result["holding_activity_status"])
        self.assertEqual(rsu_result["rsu_activity_candidate_count"], "1")
        self.assertIn("unparseable", rsu_result["rsu_activity_status"])


class HoldingReconciliationTests(unittest.TestCase):
    def test_seeded_balance_reconciles_real_statement_totals(self) -> None:
        rows = [
            row("Release", "353", "RELEASES"),
            row("Sell to Cover", "172.164720", "COVERS"),
            row("Withdrawal", "170.265929", "WITHDRAWALS"),
        ]

        closing, minimum = extractor.calculate_share_balances(rows, Decimal("7.109823"))
        extractor.apply_running_totals(rows, Decimal("7.109823"))

        self.assertEqual(closing, Decimal("17.679174"))
        self.assertGreaterEqual(minimum, Decimal("0"))
        self.assertEqual(rows[-1]["Running total"], "17.679174")

    def test_nonzero_opening_is_incomplete_even_when_balanced(self) -> None:
        result = extractor.build_holding_reconciliation(
            metadata("7.109823", "17.679174"),
            Decimal("17.679174"),
            Decimal("0"),
        )

        self.assertIn("non-zero opening holding", result["status"])
        self.assertTrue(extractor.audit_status_is_failure(result["status"]))
        self.assertEqual(result["balance_difference"], "0")

    def test_zero_opening_exact_match_is_ready(self) -> None:
        result = extractor.build_holding_reconciliation(
            metadata("0", "10"), Decimal("10"), Decimal("0")
        )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(extractor.audit_status_is_failure(result["status"]))

    def test_share_tolerance_is_inclusive(self) -> None:
        accepted = extractor.build_holding_reconciliation(
            metadata("0", "10"), Decimal("10.000001"), Decimal("0")
        )
        rejected = extractor.build_holding_reconciliation(
            metadata("0", "10"), Decimal("10.000002"), Decimal("0")
        )

        self.assertEqual(accepted["status"], "ok")
        self.assertIn("mismatch", rejected["status"])

    def test_missing_and_ambiguous_snapshots_fail(self) -> None:
        missing = extractor.build_holding_reconciliation({}, Decimal("0"), Decimal("0"))
        ambiguous_metadata = metadata("", "0")
        ambiguous_metadata["opening_snapshot_count"] = "2"
        ambiguous = extractor.build_holding_reconciliation(
            ambiguous_metadata, Decimal("0"), Decimal("0")
        )

        self.assertIn("missing: opening holding", missing["status"])
        self.assertIn("ambiguous", ambiguous["status"])

    def test_check_status_is_warning_not_failure(self) -> None:
        self.assertFalse(extractor.audit_status_is_failure("check: review this date"))
        self.assertTrue(extractor.audit_status_is_failure("missing: transaction date"))
        self.assertTrue(
            extractor.audit_status_is_failure("check: review this date; missing: quantity")
        )

    def test_equal_dates_do_not_hide_missing_withdrawal_fields(self) -> None:
        pdf_text = """
        Withdrawal on April 30, 2024
        ABC123 Reference Number:
        30-Apr-2024 Settlement Date:
        $10.00 USD Market Price Per Unit:
        """
        _, pdf_audit = extractor.parse_withdrawals(pdf_text)
        html_tables = [[
            ["Withdrawal on April 30, 2024"],
            ["Reference Number", "ABC123"],
            ["Settlement Date", "30-Apr-2024"],
            ["Market Price Per Unit", "$10.00 USD"],
        ]]
        _, html_audit = extractor.parse_html_withdrawals(html_tables)

        self.assertTrue(pdf_audit[0]["status"].startswith("missing:"))
        self.assertTrue(html_audit[0]["status"].startswith("missing:"))
        self.assertTrue(extractor.audit_status_is_failure(pdf_audit[0]["status"]))
        self.assertTrue(extractor.audit_status_is_failure(html_audit[0]["status"]))


class MainFailClosedTests(unittest.TestCase):
    def run_main(
        self,
        temp_path: Path,
        rows: list[dict[str, str]],
        audit: list[dict[str, str]],
        report_metadata: dict[str, str],
        fx_side_effect=None,
    ) -> tuple[int, str]:
        args = argparse.Namespace(
            report=temp_path / "statement.htm",
            cgt_symbol="EXMPL",
            outputs_dir=temp_path / "outputs",
            out=None,
            work_dir=temp_path / "work",
        )
        with (
            patch.object(extractor, "parse_args", return_value=args),
            patch.object(
                extractor,
                "parse_report",
                return_value=(copy.deepcopy(rows), copy.deepcopy(audit), report_metadata.copy()),
            ),
            patch.object(extractor, "apply_fx_rates") as apply_fx,
            io.StringIO() as stdout,
            patch("sys.stdout", stdout),
        ):
            def add_fx(target_rows: list[dict[str, str]]) -> None:
                for target in target_rows:
                    target["FX Rate"] = "0.8"
                    target["_fx_rate_date"] = target["_fx_lookup_date"]
                    target["_fx_source"] = "test"

            apply_fx.side_effect = fx_side_effect or add_fx
            return_code = extractor.main()
            output = stdout.getvalue()
            self.apply_fx_mock = apply_fx
        return return_code, output

    def test_ready_history_writes_cgt_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            return_code, output = self.run_main(
                temp_path,
                [row("Release", "2")],
                [],
                metadata("0", "2"),
            )

            with (temp_path / "outputs/shareworks_extracted_rows.csv").open(
                newline=""
            ) as handle:
                output_rows = list(csv.DictReader(handle))

            self.assertEqual(return_code, 0)
            self.assertIn("cgt_ready=true", output)
            self.assertTrue(output_rows[0]["CGT Calculator String"])
            self.assertIn(" EXMPL ", output_rows[0]["CGT Calculator String"])
            self.apply_fx_mock.assert_called_once()

    def test_multiple_funds_block_cgt_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            report_metadata = metadata("0", "2")
            report_metadata["fund_names"] = (
                '["Example Corporation Common Stock", "Second Corporation"]'
            )

            return_code, output = self.run_main(
                temp_path,
                [row("Release", "2")],
                [],
                report_metadata,
            )

            with (temp_path / "outputs/shareworks_extracted_rows.csv").open(
                newline=""
            ) as handle:
                output_rows = list(csv.DictReader(handle))
            audit_text = (temp_path / "outputs/shareworks_extraction_audit.csv").read_text(
                encoding="utf-8"
            )

            self.assertEqual(return_code, 1)
            self.assertIn("cgt_ready=false", output)
            self.assertEqual(output_rows[0]["CGT Calculator String"], "")
            self.assertIn("Security validation", audit_text)
            self.assertIn("ambiguous: found multiple statement funds", audit_text)
            self.apply_fx_mock.assert_not_called()

    def test_rejects_rows_and_audit_path_collision_without_touching_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            outputs_dir = temp_path / "outputs"
            outputs_dir.mkdir()
            collision_path = outputs_dir / "shareworks_extraction_audit.csv"
            collision_path.write_text("KEEP ME\n", encoding="utf-8")
            args = argparse.Namespace(
                report=temp_path / "statement.htm",
                cgt_symbol="EXMPL",
                outputs_dir=outputs_dir,
                out=collision_path,
                work_dir=temp_path / "work",
            )
            with (
                patch.object(extractor, "parse_args", return_value=args),
                patch.object(extractor, "parse_report") as parse_report,
                io.StringIO() as stdout,
                patch("sys.stdout", stdout),
            ):
                return_code = extractor.main()

            self.assertEqual(return_code, 2)
            self.assertEqual(collision_path.read_text(encoding="utf-8"), "KEEP ME\n")
            parse_report.assert_not_called()

    def test_rows_are_neutralized_before_audit_initialization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            outputs_dir = temp_path / "outputs"
            outputs_dir.mkdir()
            rows_path = outputs_dir / "shareworks_extracted_rows.csv"
            rows_path.write_text("CGT Calculator String\nSTALE READY LINE\n", encoding="utf-8")
            args = argparse.Namespace(
                report=temp_path / "statement.htm",
                cgt_symbol="EXMPL",
                outputs_dir=outputs_dir,
                out=None,
                work_dir=temp_path / "work",
            )
            real_write_csv_atomic = extractor.write_csv_atomic

            def fail_audit_write(path, fieldnames, rows):
                if path.name == "shareworks_extraction_audit.csv":
                    raise OSError("test audit write failure")
                return real_write_csv_atomic(path, fieldnames, rows)

            with (
                patch.object(extractor, "parse_args", return_value=args),
                patch.object(extractor, "write_csv_atomic", side_effect=fail_audit_write),
            ):
                with self.assertRaises(OSError):
                    extractor.main()

            self.assertNotIn("STALE READY LINE", rows_path.read_text(encoding="utf-8"))
            self.assertEqual(rows_path.read_text(encoding="utf-8").count("\n"), 1)

    def test_warning_does_not_block_ready_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            warning = {
                "source_type": "Withdrawal section",
                "id": "TEST",
                "status": "check: withdrawal date equals settlement date",
            }
            return_code, output = self.run_main(
                temp_path,
                [row("Release", "2")],
                [warning],
                metadata("0", "2"),
            )

            self.assertEqual(return_code, 0)
            self.assertIn("warning_sections=1", output)
            self.assertIn("cgt_ready=true", output)
            self.apply_fx_mock.assert_called_once()

    def test_incomplete_history_neutralizes_stale_cgt_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            outputs_dir = temp_path / "outputs"
            outputs_dir.mkdir()
            output_path = outputs_dir / "shareworks_extracted_rows.csv"
            output_path.write_text("CGT Calculator String\nSTALE READY LINE\n", encoding="utf-8")

            return_code, output = self.run_main(
                temp_path,
                [row("Release", "2")],
                [],
                metadata("1", "3"),
            )

            with output_path.open(newline="") as handle:
                output_rows = list(csv.DictReader(handle))
            audit_text = (outputs_dir / "shareworks_extraction_audit.csv").read_text(encoding="utf-8")

            self.assertEqual(return_code, 1)
            self.assertIn("cgt_ready=false", output)
            self.assertEqual(output_rows[0]["CGT Calculator String"], "")
            self.assertNotIn("STALE READY LINE", output_path.read_text(encoding="utf-8"))
            self.assertIn("non-zero opening holding", audit_text)
            self.apply_fx_mock.assert_not_called()

    def test_activity_reconciliation_failure_blocks_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            activity_failure = {
                "source_type": "Activity reconciliation",
                "id": "",
                "status": "missing: detail section for REL1",
            }

            return_code, output = self.run_main(
                temp_path,
                [row("Release", "2")],
                [activity_failure],
                metadata("0", "2"),
            )

            with (temp_path / "outputs/shareworks_extracted_rows.csv").open(newline="") as handle:
                output_rows = list(csv.DictReader(handle))

            self.assertEqual(return_code, 1)
            self.assertIn("cgt_ready=false", output)
            self.assertEqual(output_rows[0]["CGT Calculator String"], "")
            self.apply_fx_mock.assert_not_called()

    def test_fx_failure_neutralizes_partial_and_stale_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            outputs_dir = temp_path / "outputs"
            outputs_dir.mkdir()
            output_path = outputs_dir / "shareworks_extracted_rows.csv"
            audit_path = outputs_dir / "shareworks_extraction_audit.csv"
            output_path.write_text("CGT Calculator String\nSTALE READY LINE\n", encoding="utf-8")
            audit_path.write_text("status\nOLD AUDIT\n", encoding="utf-8")

            def fail_after_partial_fx(target_rows: list[dict[str, str]]) -> None:
                target_rows[0]["FX Rate"] = "0.8"
                target_rows[0]["_fx_rate_date"] = "2024-04-30"
                target_rows[0]["_fx_source"] = "partial"
                raise urllib.error.URLError("test FX outage")

            return_code, output = self.run_main(
                temp_path,
                [row("Release", "2")],
                [],
                metadata("0", "2"),
                fx_side_effect=fail_after_partial_fx,
            )

            with output_path.open(newline="") as handle:
                output_rows = list(csv.DictReader(handle))
            audit_text = audit_path.read_text(encoding="utf-8")

            self.assertEqual(return_code, 1)
            self.assertIn("cgt_ready=false", output)
            self.assertIn("fx_rates=0", output)
            self.assertNotIn("STALE READY LINE", output_path.read_text(encoding="utf-8"))
            self.assertNotIn("OLD AUDIT", audit_text)
            self.assertEqual(output_rows[0]["Running total"], "2")
            for field_name in ["FX Rate", "GBP Price", "GBP Fees", "CGT Calculator String"]:
                self.assertEqual(output_rows[0][field_name], "")
            self.assertIn("FX enrichment", audit_text)
            self.assertIn("test FX outage", audit_text)


if __name__ == "__main__":
    unittest.main()
