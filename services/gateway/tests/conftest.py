from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coding_harness_test_support import linux_process_containment_available


SERVICE_ROOT = Path(__file__).resolve().parent.parent

if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_linux_process_containment: requires readable Linux procfs child enumeration",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("requires_linux_process_containment") is None:
        return
    harness = getattr(item.module, "harness", None)
    if harness is None or not linux_process_containment_available(harness):
        pytest.skip("Linux procfs child enumeration is unavailable")
