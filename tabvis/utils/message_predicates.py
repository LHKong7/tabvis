"""Message type-guard predicates

A tool_result turn shares ``type == 'user'`` with a genuine human turn; the discriminant is
the optional ``toolUseResult`` field. Four upstream PRs (#23977, #24016, #24022, #24025)
independently fixed miscounts that came from checking ``type === 'user'`` alone, so this guard
also excludes meta messages and tool_result envelopes.

Casing: Python identifiers are snake_case. Message envelopes are plain transcript ``dict``\\ s
(see ``tabvis/types/message.py``) whose wire keys stay camelCase (``isMeta``/``toolUseResult``);
this module only reads them, never rewrites them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tabvis.types.message import Message


def is_human_turn(m: Message) -> bool:
    """Whether ``m`` is a genuine human turn (a ``user`` message that is not meta and not a
    tool_result envelope).

    Mirrors the TS type guard ``isHumanTurn``: ``type === 'user' && !m.isMeta &&
    m.toolUseResult === undefined``. In TS a tool_result envelope always carries a *defined*
    ``toolUseResult`` value, while a human turn never sets the key; serializing TS
    ``undefined`` drops the key. So ``=== undefined`` maps to ``m.get("toolUseResult") is
    None`` here — absent OR explicit-``None`` both count as "no tool result".
    """
    return (
        m.get("type") == "user"
        and not m.get("isMeta")
        and m.get("toolUseResult") is None
    )
