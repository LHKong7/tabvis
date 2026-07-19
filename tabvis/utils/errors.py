"""Error helpers

Skeleton scope: errno classification helpers used by filesystem tools. The full error-id
taxonomy is planned for a later implementation phase.
"""

from __future__ import annotations

import errno as _errno
from typing import Any


class TabvisError(Exception):
    """Base error type for tabvis."""


def get_errno_code(error: Any) -> str | None:
    """Return the errno *name* (e.g. 'ENOENT') for an OSError-like value, else None."""
    code = getattr(error, "errno", None)
    if code is None:
        return None
    try:
        return _errno.errorcode.get(code)
    except Exception:  # noqa: BLE001
        return None


def is_enoent(error: Any) -> bool:
    return getattr(error, "errno", None) == _errno.ENOENT


def is_eacces(error: Any) -> bool:
    return getattr(error, "errno", None) == _errno.EACCES


def is_eisdir(error: Any) -> bool:
    return getattr(error, "errno", None) == _errno.EISDIR


def get_error_message(error: Any) -> str:
    return str(getattr(error, "args", None) and error) or str(error)
