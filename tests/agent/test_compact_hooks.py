"""PreCompact / PostCompact hook output must reach ``compact_conversation``.

``execute_hooks`` yields ``additionalContexts`` as a LIST and never yields ``systemMessage``
directly — it wraps that text in a ``hook_system_message`` attachment. An aggregator reading the
singular/raw keys collects nothing and the hooks look like no-ops.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tabvis.utils.hooks import execute_post_compact_hooks, execute_pre_compact_hooks

HOOK_JSON = {
    "systemMessage": "compaction is about to run",
    "hookSpecificOutput": {
        "hookEventName": "PreCompact",
        "additionalContext": "Keep every URL verbatim.",
    },
}


@pytest.fixture
def pre_compact_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Register one ``PreCompact`` hook, matched on the ``auto`` trigger."""
    script = tmp_path / "hook.sh"
    script.write_text(
        "#!/bin/sh\ncat >/dev/null\ncat <<'JSON'\n" + json.dumps(HOOK_JSON) + "\nJSON\n"
    )
    os.chmod(script, 0o755)
    monkeypatch.setenv(
        "TABVIS_HOOKS",
        json.dumps(
            {
                "PreCompact": [
                    {
                        "matcher": "auto",
                        "hooks": [{"type": "command", "command": str(script)}],
                    }
                ]
            }
        ),
    )
    monkeypatch.delenv("TABVIS_SIMPLE", raising=False)


def test_pre_compact_hook_output_is_aggregated(pre_compact_hook: None) -> None:
    result = asyncio.run(execute_pre_compact_hooks({"trigger": "auto"}, None))
    assert result["newCustomInstructions"] == "Keep every URL verbatim."
    assert result["userDisplayMessage"] == "compaction is about to run"


def test_hooks_are_matched_on_the_trigger(pre_compact_hook: None) -> None:
    """The hook is registered for ``auto``; a manual compaction must not run it."""
    result = asyncio.run(execute_pre_compact_hooks({"trigger": "manual"}, None))
    assert result == {"newCustomInstructions": None, "userDisplayMessage": None}


def test_no_configured_hooks_yields_empty_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TABVIS_HOOKS", raising=False)
    result = asyncio.run(
        execute_post_compact_hooks({"trigger": "auto", "compactSummary": "s"}, None)
    )
    assert result == {"newCustomInstructions": None, "userDisplayMessage": None}
