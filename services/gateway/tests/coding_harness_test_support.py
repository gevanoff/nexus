from __future__ import annotations

import os
import sys
from types import ModuleType


def linux_process_containment_available(harness: ModuleType) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        harness._linux_direct_children(os.getpid())
    except OSError:
        return False
    return True
