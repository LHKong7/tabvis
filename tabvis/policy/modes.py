"""Permission modes → (baseline rules, fallback effect) (PP-1).

``docs/permission-policy-engine_v1.md`` §5.4. A mode is a *starting posture*: a set of built-in
baseline rules plus the effect used when no rule matches. Higher-priority sources (settings.json,
per-identity permissions, grants) are layered on top of the baseline at engine construction (§3.1);
this module only provides the built-in floor.

* ``trusted``  — fallback ``allow``; no baseline restrictions (audit still fires).
* ``standard`` — workspace/session read+write allowed; external download/upload/network/credential
  ask; writes/deletes under ``config:`` denied; fallback ``ask``.
* ``locked``   — minimal read-only allow-list; fallback ``deny``.

Pure module: no I/O, no global state.
"""

from __future__ import annotations

from typing import Literal

from tabvis.policy.rules import Effect, PolicyRule, compile_rules

Mode = Literal["trusted", "standard", "locked"]
MODES: tuple[Mode, ...] = ("trusted", "standard", "locked")

_STANDARD_BASELINE = [
    {
        "id": "std-workspace-read",
        "effect": "allow",
        "actions": ["filesystem.read"],
        "resources": ["workspace:**", "session:**", "artifact:**"],
    },
    {
        "id": "std-workspace-write",
        "effect": "allow",
        "actions": ["filesystem.write", "filesystem.delete"],
        "resources": ["workspace:**", "session:**"],
    },
    {
        "id": "std-protect-config",
        "effect": "deny",
        "actions": ["filesystem.write", "filesystem.delete"],
        "resources": ["config:**"],
    },
    {
        "id": "std-external",
        "effect": "ask",
        "actions": [
            "browser.download",
            "browser.upload",
            "network.request",
            "credential.use",
            "artifact.export",
            "clipboard.read",
            "clipboard.write",
        ],
        "resources": ["**"],
    },
]

_LOCKED_BASELINE = [
    {
        "id": "locked-workspace-read",
        "effect": "allow",
        "actions": ["filesystem.read"],
        "resources": ["workspace:**", "session:**", "artifact:**"],
    },
]

# trusted has no baseline restrictions — the fallback carries it.
_TRUSTED_BASELINE: list[dict] = []

_BASELINES: dict[str, list[dict]] = {
    "trusted": _TRUSTED_BASELINE,
    "standard": _STANDARD_BASELINE,
    "locked": _LOCKED_BASELINE,
}

_FALLBACKS: dict[str, Effect] = {
    "trusted": "allow",
    "standard": "ask",
    "locked": "deny",
}


def is_mode(value: str) -> bool:
    return value in MODES


def baseline_rules_for_mode(mode: Mode) -> list[PolicyRule]:
    """The built-in baseline rules for ``mode`` (freshly compiled each call)."""
    if mode not in _BASELINES:
        raise ValueError(f"unknown permission mode {mode!r}; expected one of {list(MODES)}")
    return compile_rules(_BASELINES[mode])


def fallback_for_mode(mode: Mode) -> Effect:
    """The effect used when no rule matches under ``mode``."""
    if mode not in _FALLBACKS:
        raise ValueError(f"unknown permission mode {mode!r}; expected one of {list(MODES)}")
    return _FALLBACKS[mode]
