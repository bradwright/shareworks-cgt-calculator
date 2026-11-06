# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdf"]
# ///

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extract_shareworks_statement import apply_derived_values, fmt_calculated, fmt_rate


class DecimalFormattingTests(unittest.TestCase):
    def test_preserves_whole_number_zeros(self):
        cases = {
            "0": "0",
            "10": "10",
            "100": "100",
            "-10": "-10",
            "1E+3": "1000",
            "10.000": "10",
            "0.000": "0",
            "1.2300": "1.23",
            "0.0100": "0.01",
            "0.79000": "0.79",
        }
        for formatter in (fmt_calculated, fmt_rate):
            with self.subTest(formatter=formatter.__name__, value=None):
                self.assertEqual(formatter(None), "")
            for value, expected in cases.items():
                with self.subTest(formatter=formatter.__name__, value=value):
                    self.assertEqual(formatter(Decimal(value)), expected)

    def test_derived_values_keep_totals_prices_and_fees(self):
        rows = [
            {
                "Date": "2024-01-02",
                "Type": transaction_type,
                "Quantity": quantity,
                "USD Price": "100",
                "USD Fees": fees,
                "FX Rate": "0.8",
            }
            for transaction_type, quantity, fees in [
                ("Release", "100", ""),
                ("Sell to Cover", "90", "25"),
                ("Withdrawal", "10", "0"),
            ]
        ]

        apply_derived_values(rows)

        self.assertEqual([row["Running total"] for row in rows], ["100", "10", "0"])
        self.assertEqual([row["GBP Price"] for row in rows], ["80", "80", "80"])
        self.assertEqual([row["GBP Fees"] for row in rows], ["0", "20", "0"])
        self.assertEqual(
            [row["CGT Calculator String"] for row in rows],
            [
                "B 02/01/2024 SHOP 100.000000 80.0000 0.00 0.00",
                "S 02/01/2024 SHOP 90.000000 80.0000 20.00 0.00",
                "S 02/01/2024 SHOP 10.000000 80.0000 0.00 0.00",
            ],
        )


if __name__ == "__main__":
    unittest.main()
