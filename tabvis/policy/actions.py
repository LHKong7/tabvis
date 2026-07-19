"""Action taxonomy for the permission policy engine (PP-1).

``docs/permission-policy-engine_v1.md`` §5.1: permissions are classified by *side-effect category*,
never by tool name — one ``filesystem.write`` covers every tool that writes a file. Actions are dotted
and match hierarchically, so a rule targeting ``filesystem`` (or ``filesystem.*``) covers
``filesystem.write``.

This module is pure data + matching; it never touches the filesystem, network, or global state.
"""

from __future__ import annotations

# The canonical action vocabulary. Rules may target any of these (or a dotted prefix / glob of them).
ACTIONS: tuple[str, ...] = (
    "filesystem.read",
    "filesystem.write",
    "filesystem.delete",
    "browser.navigate",
    "browser.interact",
    "browser.download",
    "browser.upload",
    "network.request",
    "shell.execute",
    "credential.use",
    "clipboard.read",
    "clipboard.write",
    "artifact.export",
    "runtime.read",
    "runtime.manage",
    "runtime.cancel",
    "runtime.export",
)

# Valid top-level categories, derived from ACTIONS — used to sanity-check rule patterns.
ACTION_CATEGORIES: frozenset[str] = frozenset(a.split(".", 1)[0] for a in ACTIONS)


def action_matches(pattern: str, action: str) -> bool:
    """Does ``pattern`` cover the concrete ``action``?

    Segment semantics (split on ``.``):

    * ``*``  — matches exactly one segment.
    * ``**`` — matches the remaining segments (tail wildcard).
    * a literal segment matches itself.

    A pattern that is a strict dotted *prefix* of the action also matches, so a category rule
    ``filesystem`` covers ``filesystem.write``. ``filesystem.*`` does **not** match the bare
    ``filesystem`` (the ``*`` requires a segment to consume).
    """
    if pattern == "*" or pattern == "**":
        return True
    pat = pattern.split(".")
    act = action.split(".")
    i = 0
    for i, seg in enumerate(pat):
        if seg == "**":
            return True
        if i >= len(act):
            return False
        if seg != "*" and seg != act[i]:
            return False
    # Pattern exhausted. Exact length => full match; action longer => prefix (category) match.
    return len(act) >= len(pat)


def is_known_action(action: str) -> bool:
    """True if ``action`` is one of the canonical :data:`ACTIONS` (exact match)."""
    return action in ACTIONS
