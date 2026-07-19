"""Load + compile policy rules from ``settings.json`` (PP-2).

``docs/permission-policy-engine_v1.md`` §8 PP-2: read ``permissions.rules`` from the merged settings,
compile them through the PP-1 core, and **fail loudly on an invalid rule** — a malformed rule is a
startup error, never silently ignored. This is the one impure edge that bridges the settings module
to the pure policy core; the engine itself stays side-effect free.

Priority (``design`` §3.1): the compiled settings rules layer *above* the mode baseline and *below*
per-identity permissions and grants. :func:`build_policy_engine` wires that order.
"""

from __future__ import annotations

import os
from typing import Any

from tabvis.policy.engine import PolicyEngine
from tabvis.policy.modes import MODES, Mode
from tabvis.policy.rules import PolicyConfigError, PolicyRule, compile_rules

_DEFAULT_MODE: Mode = "standard"

# First-class one-off switch (PP-4). Set for a single run to tighten (CI → ``locked``) or loosen
# (local → ``trusted``) without editing settings. Mirrors the ``TABVIS_BROWSER_*`` env convention, so
# it is the CLI-equivalent knob in this headless codebase.
_MODE_ENV = "TABVIS_PERMISSION_MODE"

# Shadow mode (PP-5): compute + audit decisions but never block — every non-allow is served as allow.
# Use it to measure real ``ask``/``deny`` frequency before switching a policy to enforcing.
_SHADOW_ENV = "TABVIS_PERMISSION_SHADOW"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_shadow_mode() -> bool:
    """Whether ``TABVIS_PERMISSION_SHADOW`` is set truthy (audit-only, never enforce)."""
    val = os.environ.get(_SHADOW_ENV)
    return bool(val) and val.strip().lower() in _TRUTHY


def _raw_rules_from_permissions(permissions: Any) -> list[Any]:
    """Pull the raw ``rules`` list off a permissions object (typed field or ``extra`` passthrough)."""
    if permissions is None:
        return []
    raw = getattr(permissions, "rules", None)
    if raw is None:
        extra = getattr(permissions, "model_extra", None)
        if isinstance(extra, dict):
            raw = extra.get("rules")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PolicyConfigError("settings.json permissions.rules must be a list")
    return raw


def load_policy_rules_from_settings(settings: Any | None = None) -> list[PolicyRule]:
    """Compile ``permissions.rules`` from settings into :class:`PolicyRule` objects.

    Reads the session-merged settings when ``settings`` is None. Raises :class:`PolicyConfigError`
    (annotated with the settings source) if any rule is malformed — the caller must surface this at
    startup, not swallow it.
    """
    if settings is None:
        from tabvis.utils.settings.settings import get_initial_settings

        settings = get_initial_settings()

    permissions = getattr(settings, "permissions", None)
    raw = _raw_rules_from_permissions(permissions)
    if not raw:
        return []
    try:
        return compile_rules(raw)
    except PolicyConfigError as exc:
        raise PolicyConfigError(f"invalid settings.json permissions.rules: {exc}") from exc


def resolve_mode(settings: Any | None = None) -> Mode:
    """Effective permission mode: env override > ``settings.json`` ``permissions.mode`` > default.

    ``TABVIS_PERMISSION_MODE`` (case-insensitive) is the first-class one-off switch; when unset the
    settings-file value applies, else ``standard``. An unrecognized value from either source is a
    config error, never a silent fallback. The headless ``ask``→deny posture is unchanged — it is
    enforced downstream of the decision, independent of the mode.
    """
    raw = os.environ.get(_MODE_ENV)
    if raw is not None and raw.strip():
        val = raw.strip().lower()
        if val not in MODES:
            raise PolicyConfigError(f"invalid {_MODE_ENV}={raw!r}; expected one of {list(MODES)}")
        return val  # type: ignore[return-value]
    return read_mode_from_settings(settings)


def read_mode_from_settings(settings: Any | None = None) -> Mode:
    """Read ``permissions.mode`` (trusted/standard/locked), defaulting to ``standard``.

    The settings-file half of :func:`resolve_mode`. An unrecognized mode value is a config error, not
    a silent fallback.
    """
    if settings is None:
        from tabvis.utils.settings.settings import get_initial_settings

        settings = get_initial_settings()
    permissions = getattr(settings, "permissions", None)
    mode = getattr(permissions, "mode", None) if permissions is not None else None
    if mode is None:
        extra = getattr(permissions, "model_extra", None) if permissions is not None else None
        if isinstance(extra, dict):
            mode = extra.get("mode")
    if mode is None:
        return _DEFAULT_MODE
    if mode not in MODES:
        raise PolicyConfigError(
            f"invalid settings.json permissions.mode {mode!r}; expected one of {list(MODES)}"
        )
    return mode  # type: ignore[return-value]


def build_policy_engine(
    mode: Mode | None = None,
    settings: Any | None = None,
    extra_rules: list[PolicyRule] | None = None,
) -> PolicyEngine:
    """Build an engine: mode baseline < settings rules < ``extra_rules`` (identity/grants).

    ``mode`` defaults to :func:`resolve_mode` (env override > settings > default). ``extra_rules``
    (already ordered by priority) layer on top of the settings rules, so a grant can still upgrade a
    baseline ``ask``.
    """
    resolved_mode: Mode = mode if mode is not None else resolve_mode(settings)
    layered = load_policy_rules_from_settings(settings)
    if extra_rules:
        layered = layered + list(extra_rules)
    return PolicyEngine.for_mode(resolved_mode, extra_rules=layered)
