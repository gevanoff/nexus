from __future__ import annotations

import unittest

from math_tools import summarize_numbers


class SummarizeNumbersTests(unittest.TestCase):
    def test_odd_length_summary(self) -> None:
        self.assertEqual(
            summarize_numbers([5, 1, 3]),
            {"count": 3.0, "total": 9.0, "mean": 3.0, "median": 3.0},
        )

    def test_even_length_summary_uses_average_of_middle_values(self) -> None:
        self.assertEqual(
            summarize_numbers([4, 1, 2, 3]),
            {"count": 4.0, "total": 10.0, "mean": 2.5, "median": 2.5},
        )

    def test_empty_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            summarize_numbers([])


if __name__ == "__main__":
    unittest.main()
