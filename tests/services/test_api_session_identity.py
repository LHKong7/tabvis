"""API headers and SDK messages must follow the active run session."""

from __future__ import annotations

from tabvis.agent.api import client
from tabvis.bootstrap.state import get_session_id, get_session_project_dir, switch_session
from tabvis.types.ids import as_session_id
from tabvis.utils.query_helpers import normalize_message


def test_api_and_normalized_messages_follow_session_switch() -> None:
    previous_id = get_session_id()
    previous_project_dir = get_session_project_dir()
    expected = as_session_id("session-under-test")
    switch_session(expected, project_dir="/tmp/session-under-test")
    try:
        assert client.get_session_id() == expected
        message = {
            "type": "assistant",
            "uuid": "assistant-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
            },
        }
        [normalized] = list(normalize_message(message))
        assert normalized["session_id"] == expected
    finally:
        switch_session(previous_id, project_dir=previous_project_dir)
