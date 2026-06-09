from __future__ import annotations

import unittest

from inventory_tools import build_reorder_plan


class BuildReorderPlanTests(unittest.TestCase):
    def test_normalizes_and_aggregates_duplicate_skus(self) -> None:
        rows = [
            {"sku": " abc-1 ", "on_hand": 2, "target": 5},
            {"sku": "ABC-1", "on_hand": 1, "target": 5},
            {"sku": "xyz-9", "on_hand": 10, "target": 4},
        ]

        self.assertEqual(build_reorder_plan(rows), [{"sku": "ABC-1", "quantity": 2}])

    def test_returns_sorted_positive_reorders_only(self) -> None:
        rows = [
            {"sku": "kit-2", "on_hand": 0, "target": 3},
            {"sku": "part-7", "on_hand": 9, "target": 9},
            {"sku": "box-1", "on_hand": 1, "target": 3},
        ]

        self.assertEqual(
            build_reorder_plan(rows),
            [{"sku": "BOX-1", "quantity": 2}, {"sku": "KIT-2", "quantity": 3}],
        )

    def test_ignores_blank_skus(self) -> None:
        rows = [
            {"sku": " ", "on_hand": 0, "target": 5},
            {"sku": "valid", "on_hand": 1, "target": 2},
        ]

        self.assertEqual(build_reorder_plan(rows), [{"sku": "VALID", "quantity": 1}])


if __name__ == "__main__":
    unittest.main()
