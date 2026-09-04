from __future__ import annotations

import os
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
    config.addinivalue_line(
        "markers",
        "requires_non_root_validation: validation task limits require a non-root host user",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    if (
        item.get_closest_marker("requires_non_root_validation") is not None
        and (
            getattr(os, "getuid", lambda: 0)() == 0
            or getattr(os, "geteuid", lambda: 0)() == 0
        )
    ):
        pytest.skip("validation task limits require a non-root host user")
    if item.get_closest_marker("requires_linux_process_containment") is None:
        return
    harness = getattr(item.module, "harness", None)
    if harness is None or not linux_process_containment_available(harness):
        pytest.skip("Linux procfs child enumeration is unavailable")
