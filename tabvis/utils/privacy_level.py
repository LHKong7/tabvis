"""Privacy / network-traffic level

Privacy level controls how much nonessential network traffic and telemetry Tabvis generates.
Levels are ordered by restrictiveness::

    default < no-telemetry < essential-traffic

* ``default``           — Everything enabled.
* ``no-telemetry``      — Analytics/telemetry disabled (Datadog, 1P events, feedback survey).
* ``essential-traffic`` — ALL nonessential network traffic disabled (telemetry + auto-updates,
  grove, release notes, model capabilities, etc.).

The resolved level is the most restrictive signal from the environment::

    TABVIS_DISABLE_NONESSENTIAL_TRAFFIC  ->  essential-traffic
    DISABLE_TELEMETRY                  ->  no-telemetry

Casing: Python identifiers are snake_case; the level string literals keep their TS spelling
(``'default'``/``'no-telemetry'``/``'essential-traffic'``) since they are the value contract.

Faithful behavior notes:
- The env checks mirror the TS ``process.env.X`` truthiness exactly: a present-but-empty
  string (``""``) is FALSY in JS, so the Python implementation uses ``os.environ.get(...)`` truthiness
  (``""`` and unset both read as "not set"), not mere key presence.
"""

from __future__ import annotations

import os
from typing import Literal

PrivacyLevel = Literal["default", "no-telemetry", "essential-traffic"]


def get_privacy_level() -> PrivacyLevel:
    """Resolve the most restrictive privacy level from the environment.

    Return the privacy level.
    """
    if os.environ.get("TABVIS_DISABLE_NONESSENTIAL_TRAFFIC"):
        return "essential-traffic"
    if os.environ.get("DISABLE_TELEMETRY"):
        return "no-telemetry"
    return "default"


def is_essential_traffic_only() -> bool:
    """True when all nonessential network traffic should be suppressed.

    Equivalent to the old ``process.env.TABVIS_DISABLE_NONESSENTIAL_TRAFFIC`` check.
    ``isEssentialTrafficOnly``.
    """
    return get_privacy_level() == "essential-traffic"


def is_telemetry_disabled() -> bool:
    """True when telemetry/analytics should be suppressed.

    True at both ``no-telemetry`` and ``essential-traffic`` levels.
    ``isTelemetryDisabled``.
    """
    return get_privacy_level() != "default"


def get_essential_traffic_only_reason() -> str | None:
    """Return the env var name responsible for the current essential-traffic restriction.

    Returns ``None`` if unrestricted. Used for user-facing "unset X to re-enable" messages.
    Return the essential traffic only reason.
    """
    if os.environ.get("TABVIS_DISABLE_NONESSENTIAL_TRAFFIC"):
        return "TABVIS_DISABLE_NONESSENTIAL_TRAFFIC"
    return None
