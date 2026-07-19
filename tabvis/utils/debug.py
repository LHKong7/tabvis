"""Debug logging

Skeleton scope: ``log_for_debugging`` writes to stderr only when TABVIS_DEBUG/DEBUG is set.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def is_debug_enabled() -> bool:
    return bool(os.environ.get("TABVIS_DEBUG") or os.environ.get("DEBUG"))


def log_for_debugging(*args: Any) -> None:
    if is_debug_enabled():
        print(*args, file=sys.stderr)
