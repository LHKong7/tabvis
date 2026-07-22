"""Shared helpers for the ``Browser*`` tool family.

Keeps the individual tool modules thin: availability gating, dict-or-model input access, the
observation → ``tool_result`` block mapping (text, optional screenshot image block, or an error
block), and the navigation permission decision (allowlist-first, but empty allowlist => allow
all, per the configured posture).
"""

from __future__ import annotations

import json
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse

from tabvis.tool import ToolUseContext
from tabvis.types.permissions import PermissionDecision
from tabvis.utils.browser_config import (
    get_browser_allowed_domains,
    playwright_available,  # noqa: F401 - re-exported; each tool's is_enabled() uses it
)


async def sync_browser_session(
    context: ToolUseContext,
    data: dict[str, Any],
    *,
    event: dict[str, Any] | None = None,
) -> None:
    """After a browser action: persist the session record, publish app_state, record the artifact.

    Records the navigation (only on a real URL change), refreshes the tab list, rewrites
    ``browser-session.json``, and publishes a compact JSON-safe summary onto
    ``app_state["browserSession"]`` so any tool can see which browser the agent is driving. When
    ``event`` is given (the action-specific fields — type/action/url/interaction) the browsing trail
    is also recorded via the artifacts store. Best-effort throughout — bookkeeping must never fail a
    browser action.
    """
    from tabvis.browser.manager import get_session_summary, record_activity

    try:
        await record_activity(url=data.get("url"), title=data.get("title"))
        summary = get_session_summary()
        if context.set_app_state and summary:
            # set_app_state takes an updater (prev) -> next, and the store short-circuits on
            # identity — so always return a FRESH dict.
            context.set_app_state(
                lambda prev: {**prev, "browserSession": summary}
                if isinstance(prev, dict)
                else prev
            )
    except Exception:  # noqa: BLE001 - bookkeeping is best-effort
        pass

    if event is not None:
        try:
            from tabvis.browser.artifacts import record_browser_artifact

            await record_browser_artifact(event, data)
        except Exception:  # noqa: BLE001 - recording the trail is best-effort
            pass

        # OBS-3: publish a raw ``action.performed`` event onto the EventBus (dual-write with the
        # artifact log above). No-op unless TABVIS_BROWSER_EVENT_BUS is on, so the default path is
        # unchanged. The pipeline (normalizer → timeline → forward) is installed lazily here.
        try:
            from tabvis.browser.event_bus import get_event_bus, is_event_bus_enabled

            if is_event_bus_enabled():
                from tabvis.bootstrap.state import get_session_id
                from tabvis.browser.events import RawEventType, RuntimeEvent
                from tabvis.browser.manager import current_agent_id
                from tabvis.browser.observation import install_observation_pipeline

                install_observation_pipeline()
                interaction = event.get("interaction") or {}
                await get_event_bus().publish(
                    RuntimeEvent(
                        type=RawEventType.ACTION_PERFORMED,
                        source="playwright",
                        payload={
                            "event_type": event.get("type"),
                            "action": event.get("action"),
                            "url": event.get("url") or data.get("url"),
                            "title": data.get("title"),
                            "tab_count": data.get("tab_count"),
                            "ref": interaction.get("ref"),
                            "intent": data.get("intent"),
                        },
                        agent_id=current_agent_id(),
                        session_id=str(get_session_id()),
                    )
                )
        except Exception:  # noqa: BLE001 - the bus is best-effort
            pass

        # INT-5: when the intent surface is on, record a low-level tool action as an execution too, so
        # execution_id / policy / emit are uniform across the intent tool and the five low-level tools.
        # The intent tool passes its own ``_execution_id``, so this only mints for the low-level path.
        try:
            from tabvis.browser.intents.router import is_browser_intents_enabled

            if is_browser_intents_enabled() and not event.get("_execution_id"):
                from tabvis.browser.intents.execution_registry import get_execution_registry
                from tabvis.browser.intents.types import (
                    ExecutionRecord,
                    new_execution_id,
                )
                from tabvis.browser.session import utc_now

                # Store a trimmed COPY, not the live dict: drop the heavy/sensitive screenshot + raw
                # HTML so GET /v1/executions/{id} does not echo them back.
                observation = {
                    k: v for k, v in data.items() if k not in ("screenshot_b64", "html")
                }
                get_execution_registry().record(
                    ExecutionRecord(
                        execution_id=new_execution_id(),
                        intent=str(event.get("action") or event.get("type") or "action"),
                        status="completed",
                        ended_at=utc_now(),
                        observation=observation,
                    )
                )
        except Exception:  # noqa: BLE001 - execution recording is best-effort
            pass


def get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or a pydantic model instance (defensive — callers vary)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def observation_to_block(data: dict[str, Any], tool_use_id: str) -> dict[str, Any]:
    """Map a BrowserService observation dict to an Anthropic ``tool_result`` block.

    - ``{"error": msg}``   -> an ``is_error`` text block (the model recovers).
    - screenshot present   -> a list of ``[text, image]`` blocks.
    - otherwise            -> a text block (header + snapshot).

    When the accessibility snapshot was too sparse to reason from (``aria_thin``), the observation
    also carries a screenshot and a trimmed ``html`` copy of the page; both are folded in so the
    model can fall back on the visual + structural view.
    """
    error = data.get("error")
    if error:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": str(error),
            "is_error": True,
        }

    header = (
        f"URL: {data.get('url', '')}\n"
        f"Title: {data.get('title', '')}\n"
        f"Open tabs: {data.get('tab_count', 1)}\n\n"
    )
    action_result = data.get("action_result")
    if isinstance(action_result, dict):
        header += "Action result: " + json.dumps(action_result, ensure_ascii=False) + "\n\n"
    text = header + (data.get("snapshot") or "")
    html = data.get("html")
    if html:
        text += (
            "\n\n--- page HTML (the accessibility snapshot was sparse — the raw HTML and a "
            "screenshot are included so you can still reason about this page) ---\n" + html
        )
    screenshot_b64 = data.get("screenshot_b64")
    if screenshot_b64:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "data": screenshot_b64,
                        "media_type": "image/png",
                    },
                },
            ],
        }
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}


def _host_matches(host: str, pattern: str) -> bool:
    host = (host or "").lower()
    pattern = (pattern or "").lower().strip()
    if pattern.startswith("domain:"):
        pattern = pattern[len("domain:") :]
    if not pattern or not host:
        return False
    return host == pattern or fnmatch(host, pattern)


def check_navigation_permission(
    tool_name: str, input: Any, context: ToolUseContext
) -> PermissionDecision:
    """Allow navigation to any host by default; restrict only when an allowlist is configured.

    Back/forward/reload act on already-visited pages and are always allowed. For ``goto``, if a
    non-empty allowlist is configured the target host must match (exact or ``*.example.com``
    wildcard); otherwise the decision is ``ask`` (which the headless gate resolves to deny) with
    an addRules suggestion telling the user exactly what to allowlist.
    """
    action = get_field(input, "action") or "goto"
    if action != "goto":
        return {"behavior": "allow", "updatedInput": input}

    url = get_field(input, "url")
    allowed = get_browser_allowed_domains()
    if not allowed:
        # Empty allowlist => allow all domains (the configured posture).
        return {"behavior": "allow", "updatedInput": input}

    host = ""
    try:
        host = urlparse(url or "").hostname or ""
    except ValueError:
        host = ""
    if host and any(_host_matches(host, pat) for pat in allowed):
        return {"behavior": "allow", "updatedInput": input}

    rule_content = f"domain:{host}" if host else f"input:{url}"
    return {
        "behavior": "ask",
        "message": (
            f"Tabvis wants to navigate to {host or url!r}, which is not in the browser domain "
            f"allowlist. Add it to allow this navigation."
        ),
        "suggestions": [
            {
                "type": "addRules",
                "destination": "localSettings",
                "rules": [{"toolName": tool_name, "ruleContent": rule_content}],
                "behavior": "allow",
            }
        ],
    }
