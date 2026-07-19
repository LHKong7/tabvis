"""Logging helpers

Skeleton scope: stderr-backed ``log_error``/``log_info`` (gated, low-noise). The structured
error-log sink + telemetry wiring is planned for a later implementation phase.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def log_error(error: Any, *, prefix: str | None = None) -> None:
    msg = str(error)
    print(f"{prefix + ': ' if prefix else ''}{msg}", file=sys.stderr)


def log_info(message: str) -> None:
    if os.environ.get("TABVIS_DEBUG") or os.environ.get("DEBUG"):
        print(message, file=sys.stderr)
