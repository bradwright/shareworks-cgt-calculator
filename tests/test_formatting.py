# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pypdf",
# ]
# ///

import sys
import unittest
from decimal import Decimal
from pathlib import Path

# Add project root to sys.path so src can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extract_shareworks_statement import fmt_decimal


class TestFormatting(unittest.TestCase):
    def test_fmt_decimal_integers_ending_in_zero(self):
        """Ensure integers ending in zero are not corrupted by rstrip('0')."""
        self.assertEqual(fmt_decimal(Decimal("270")), "270")
        self.assertEqual(fmt_decimal(Decimal("100")), "100")
        self.assertEqual(fmt_decimal(Decimal("10")), "10")

    def test_fmt_decimal_trailing_fractional_zeros(self):
        """Ensure trailing zeros after a decimal point are stripped."""
        self.assertEqual(fmt_decimal(Decimal("270.00")), "270")
        self.assertEqual(fmt_decimal(Decimal("270.50")), "270.5")
        self.assertEqual(fmt_decimal(Decimal("1.50000")), "1.5")

    def test_fmt_decimal_exact_fractions(self):
        """Ensure non-zero fractional digits are preserved."""
        self.assertEqual(fmt_decimal(Decimal("124.13112")), "124.13112")
        self.assertEqual(fmt_decimal(Decimal("0.81794")), "0.81794")

    def test_fmt_decimal_zero_and_none(self):
        """Ensure zero and None handle correctly."""
        self.assertEqual(fmt_decimal(None), "")
        self.assertEqual(fmt_decimal(Decimal("0")), "0")
        self.assertEqual(fmt_decimal(Decimal("0.00")), "0")


if __name__ == "__main__":
    unittest.main()
