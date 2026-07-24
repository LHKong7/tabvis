"""Browser Host — owns the browser, gates Agent RPCs during authentication (design §4.2, §13).

The Browser Host is the process that actually holds the browser context. In production (L2) the Agent
only reaches it through a narrow RPC and never gets the CDP endpoint, profile directory, cookies or
storage (design §4.2). This module provides the *gating* core that makes that boundary observable:

* :func:`agent_rpc_allowed` / :func:`guard_agent_rpc` — an ordinary Agent browser RPC is refused with
  ``browser_authentication_locked`` whenever an authentication lease is active (§4.2, §13.1);
* :func:`begin_authentication` — acquires the exclusive lease and returns a restricted
  :class:`~tabvis.browser.auth_browser.RestrictedAuthenticationBrowser` bound to the current context,
  which is the ONLY browser surface the Executor gets;
* on any exit the lease is released and, if field-clearing could not be confirmed, the caller is told
  to destroy the context (§13.3 "无法确认字段已清理时必须销毁整个 Context").

The Playwright wiring of the real host is layered on top; this is the security-relevant control logic,
tested with a fake :class:`~tabvis.browser.auth_browser.PageController`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from tabvis.browser import auth_lease
from tabvis.browser.auth_browser import PageController, RestrictedAuthenticationBrowser

# The stable code an ordinary Agent browser RPC gets while authentication holds the browser (§4.2).
BROWSER_AUTHENTICATION_LOCKED = "browser_authentication_locked"


def agent_rpc_allowed(browser_session_id: str | None = None) -> bool:
    """Whether an ordinary Agent browser RPC may run right now.

    False while any authentication lease is active (or the given session's lease specifically). Ordinary
    navigation, click, type, snapshot, download and JS are all blocked during authentication (§13.1).
    """
    if browser_session_id is not None and auth_lease.is_authentication_locked(browser_session_id):
        return False
    return not auth_lease.any_authentication_locked()


def guard_agent_rpc(browser_session_id: str | None = None) -> None:
    """Raise :class:`PermissionError('browser_authentication_locked')` if an RPC is currently blocked."""
    if not agent_rpc_allowed(browser_session_id):
        raise PermissionError(BROWSER_AUTHENTICATION_LOCKED)


@asynccontextmanager
async def begin_authentication(
    controller: PageController,
    *,
    browser_session_id: str,
    task_id: str,
    request_id: str,
):
    """Acquire the auth lease and yield a restricted browser bound to the current context (§4.2, §7.1).

    On exit the lease is always released. If clearing the sensitive fields raises, the context is marked
    for destruction (yielded object's ``must_destroy_context`` is set) so the caller tears it down
    rather than returning a browser with secrets possibly still in fields (§13.3).
    """
    context = await controller.current_context()
    lease = auth_lease.acquire(
        browser_session_id, task_id=task_id, request_id=request_id
    )
    session = _AuthSession(RestrictedAuthenticationBrowser(controller, context))
    try:
        yield session
    finally:
        try:
            await controller.clear_fields()
        except Exception:  # noqa: BLE001 - could not confirm the fields were cleared
            session.must_destroy_context = True
        lease.release()


class _AuthSession:
    """Handle yielded by :func:`begin_authentication`."""

    def __init__(self, browser: RestrictedAuthenticationBrowser) -> None:
        self.browser = browser
        self.must_destroy_context = False
