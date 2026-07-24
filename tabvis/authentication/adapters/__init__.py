"""Authentication adapters (design §9).

An adapter drives one site's login form using the *restricted* :class:`AuthenticationBrowser` — it
never receives a full Playwright ``BrowserContext``, cannot read field values / cookies / storage,
cannot run arbitrary JS, and cannot capture screenshots or DOM (design §6.3). Adapters are loaded from
a static, versioned registry (§9.1); a profile names one by id and can never point at an arbitrary
module or script.
"""

from __future__ import annotations

from tabvis.authentication.adapters.base import (
    AdapterAuthenticationResult,
    AuthenticationAdapter,
    AuthenticationBrowser,
    AuthenticationFieldHandle,
    AuthenticationFieldHints,
    AuthenticationSuccessCondition,
)
from tabvis.authentication.adapters.registry import get_adapter, is_registered_adapter

__all__ = [
    "AdapterAuthenticationResult",
    "AuthenticationAdapter",
    "AuthenticationBrowser",
    "AuthenticationFieldHandle",
    "AuthenticationFieldHints",
    "AuthenticationSuccessCondition",
    "get_adapter",
    "is_registered_adapter",
]
