"""Ordinary browser tools are refused during authentication (design §4.2, §13.1)."""

from __future__ import annotations

from types import SimpleNamespace

from tabvis.browser import auth_lease
from tabvis.browser.policy_guard import evaluate
from tabvis.constants.tools import (
    BROWSER_AUTHENTICATE_TOOL_NAME,
    BROWSER_SNAPSHOT_TOOL_NAME,
    BROWSER_TYPE_TOOL_NAME,
)


def _ctx(session_id: str | None = None):
    return SimpleNamespace(agent_id="ag", tool_use_id="tu", browser_session_id=session_id)


def test_browser_tools_denied_while_locked() -> None:
    lease = auth_lease.acquire("b1", task_id="t1", request_id="r1")
    try:
        decision = evaluate(BROWSER_TYPE_TOOL_NAME, {"ref": "e1", "text": "x"}, _ctx("b1"))
        assert decision["behavior"] == "deny"
        assert decision["message"] == "browser_authentication_locked"

        snap = evaluate(BROWSER_SNAPSHOT_TOOL_NAME, {}, _ctx("b1"))
        assert snap["behavior"] == "deny"
    finally:
        lease.release()


def test_any_session_lock_blocks_contextless_calls() -> None:
    lease = auth_lease.acquire("b9", task_id="t1", request_id="r1")
    try:
        # a call whose context carries no session id is still blocked by any active lease
        decision = evaluate(BROWSER_SNAPSHOT_TOOL_NAME, {}, _ctx(None))
        assert decision["behavior"] == "deny"
    finally:
        lease.release()


def test_authenticate_tool_is_exempt() -> None:
    lease = auth_lease.acquire("b1", task_id="t1", request_id="r1")
    try:
        # the authentication tool itself must not be blocked by the lock it will create
        decision = evaluate(
            BROWSER_AUTHENTICATE_TOOL_NAME, {"credential_profile_id": "p1"}, _ctx("b1")
        )
        assert decision["behavior"] != "deny" or decision.get("message") != "browser_authentication_locked"
    finally:
        lease.release()


def test_not_locked_allows_normally() -> None:
    # with no lease, a normal browser tool is evaluated as usual (not force-denied)
    decision = evaluate(BROWSER_SNAPSHOT_TOOL_NAME, {}, _ctx("b1"))
    assert decision["behavior"] in ("allow", "ask", "deny")
    # and specifically not our lock message
    assert decision.get("message") != "browser_authentication_locked"
