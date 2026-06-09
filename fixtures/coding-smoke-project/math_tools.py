from __future__ import annotations


def summarize_numbers(values: list[float]) -> dict[str, float]:
    """Return count, total, mean, and median for a non-empty list of numbers."""
    if not values:
        raise ValueError("values must not be empty")

    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    total = sum(ordered)
    midpoint = count // 2

    if count % 2 == 1:
        median = ordered[midpoint]
    else:
        median = ordered[midpoint]

    return {
        "count": float(count),
        "total": total,
        "mean": total / count,
        "median": median,
    }
