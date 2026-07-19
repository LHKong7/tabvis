"""Git-related behaviors that depend on user settings

The TS module lives outside ``git.ts`` to avoid a settings/git import cycle (and to keep
``git.ts`` out of the vscode-extension dep graph). The Python implementation carries the same single
gate: whether the system prompt should include git instructions.

``should_include_git_instructions`` honors ``TABVIS_DISABLE_GIT_INSTRUCTIONS`` first (truthy →
``False``, defined-falsy → ``True``), then falls back to the ``includeGitInstructions`` settings
key (camelCase wire key), defaulting to ``True``.

Casing: Python identifiers are snake_case. The settings ``includeGitInstructions`` key is a
camelCase wire key, read from the loose ``SettingsJson`` (which preserves unknown keys via
``extra="allow"``), so the wire name is kept verbatim when reading it back.
"""

from __future__ import annotations

import os

from tabvis.utils.env_utils import is_env_defined_falsy, is_env_truthy


def should_include_git_instructions() -> bool:
    """Whether the system prompt should include git instructions.

    Precedence (matches the TS ``shouldIncludeGitInstructions``):
    1. ``TABVIS_DISABLE_GIT_INSTRUCTIONS`` truthy → ``False``.
    2. ``TABVIS_DISABLE_GIT_INSTRUCTIONS`` defined-falsy → ``True``.
    3. else → settings ``includeGitInstructions`` (default ``True``).
    """
    env_val = os.environ.get("TABVIS_DISABLE_GIT_INSTRUCTIONS")
    if is_env_truthy(env_val):
        return False
    if is_env_defined_falsy(env_val):
        return True

    # Lazy import (cycle-safety parity with the TS settings import + mirrors how the other
    # settings consumers in this tree pull get_initial_settings in a function-local import).
    from tabvis.utils.settings.settings import get_initial_settings

    settings = get_initial_settings()
    # ``includeGitInstructions`` is not an explicit field on the loose SettingsJson model; it is
    # preserved verbatim (camelCase wire key) via ``extra="allow"``. Read it back by its wire name.
    include = getattr(settings, "includeGitInstructions", None)
    if include is None:
        include = (settings.model_extra or {}).get("includeGitInstructions")
    # TS: `?? true` — only a literal null/undefined falls through to the default.
    return True if include is None else bool(include)
