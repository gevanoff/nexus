from __future__ import annotations

from typing import Iterable, Mapping


def build_reorder_plan(rows: Iterable[Mapping[str, object]]) -> list[dict[str, int | str]]:
    """Return sorted reorder quantities by normalized SKU."""
    plan: list[dict[str, int | str]] = []

    for row in rows:
        sku = str(row.get("sku", ""))
        on_hand = int(row.get("on_hand", 0))
        target = int(row.get("target", 0))
        quantity = target - on_hand
        if quantity > 0:
            plan.append({"sku": sku, "quantity": quantity})

    return plan
