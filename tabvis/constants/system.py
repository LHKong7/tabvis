"""Critical system constants (sysprompt prefixes + attribution).

Defines the three CLI sysprompt prefix strings + their lookup set, the prefix selector, and the
attribution-header helpers. First-party attribution headers are not emitted by this build, so
:func:`getAttributionHeader` returns ``''`` (the ``fingerprint`` arg is accepted but ignored).

Casing: ``getCLISyspromptPrefix`` / ``getAttributionHeader`` keep camelCase names intentionally
(lint-exempt). The prefix *string values* round-trip verbatim into the system prompt.
"""

from __future__ import annotations

import os
from tabvis.utils.env_utils import is_env_defined_falsy

DEFAULT_PREFIX = "You are Tabvis, a browser agent that operates a real web browser to accomplish tasks on the web."
AGENT_SDK_TABVIS_PRESET_PREFIX = (
    "You are Tabvis, a browser agent running within the agent SDK."
)
AGENT_SDK_PREFIX = "You are an agent built on the agent SDK."

_CLI_SYSPROMPT_PREFIX_VALUES = (
    DEFAULT_PREFIX,
    AGENT_SDK_TABVIS_PRESET_PREFIX,
    AGENT_SDK_PREFIX,
)

# ``CLISyspromptPrefix`` is the union of the three prefix strings.
CLISyspromptPrefix = str

# All possible CLI sysprompt prefix values, used by ``splitSysPromptPrefix`` to identify prefix
# blocks by content rather than position.
CLI_SYSPROMPT_PREFIXES: frozenset[str] = frozenset(_CLI_SYSPROMPT_PREFIX_VALUES)


def getCLISyspromptPrefix(  # noqa: N802 - camelCase kept intentionally
    options: dict | None = None,
) -> str:
    """``options`` is ``{isNonInteractive: bool, hasAppendSystemPrompt: bool}`` (or ``None``).
    Returns the agent-SDK preset prefix for a non-interactive run with an appended system prompt,
    the bare agent-SDK prefix for a non-interactive run otherwise, and the default prefix
    interactively.
    """
    if options is not None and options.get("isNonInteractive"):
        if options.get("hasAppendSystemPrompt"):
            return AGENT_SDK_TABVIS_PRESET_PREFIX
        return AGENT_SDK_PREFIX
    return DEFAULT_PREFIX


def _is_attribution_header_enabled() -> bool:
    """Enabled by default; disabled via env or killswitch.

    Off when ``TABVIS_ATTRIBUTION_HEADER`` is defined-falsy; otherwise on (the GrowthBook
    ``tengu_attribution_header`` flag defaults to ``True``).
    """
    if is_env_defined_falsy(os.environ.get("TABVIS_ATTRIBUTION_HEADER")):
        return False
    return True


def getAttributionHeader(  # noqa: N802 - camelCase kept intentionally
    fingerprint: str,
) -> str:
    """First-party attribution headers are not emitted by this build, so this returns ``''``
    (the ``fingerprint`` arg is accepted and ignored).
    """
    _ = fingerprint
    return ""


__all__ = [
    "AGENT_SDK_TABVIS_PRESET_PREFIX",
    "AGENT_SDK_PREFIX",
    "CLI_SYSPROMPT_PREFIXES",
    "CLISyspromptPrefix",
    "DEFAULT_PREFIX",
    "getAttributionHeader",
    "getCLISyspromptPrefix",
]
