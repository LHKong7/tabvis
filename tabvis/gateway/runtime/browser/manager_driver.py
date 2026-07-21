"""ManagerBrowserDriver — the BrowserDriver over tabvis's real browser subsystem (design §10).

Bridges the Browser Runtime's binding interface to the existing `tabvis/browser` manager and
`BrowserService`: `launch` reserves the agent's workspace (`init_browser_session`), `execute` drives
the live page (`BrowserService.navigate` / `.snapshot`) and shapes the observation into the runtime's
result dict, `verify_identity` checks the session is still live, and `close` quits the agent's browser.

The manager is keyed by ``agent_id`` (via a ContextVar), so the driver records the ``profile_key →
agent_id`` map at launch. Every touchpoint to the real subsystem is an injectable hook whose default is
the real manager function — so the driver is unit-tested with a fake service, no Chromium required, the
same seam pattern used for the agent-loop launcher.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime.browser.contracts import BrowserIntent, DriverSpec

# Injectable hook types (defaults bind to tabvis.browser.manager).
Initializer = Callable[[DriverSpec, str, str], None]         # (spec, model, cwd) -> None
ServiceProvider = Callable[[str], Awaitable[Any]]            # (agent_id) -> BrowserService
Verifier = Callable[[str], bool]                            # (agent_id) -> bool
Closer = Callable[[str], Awaitable[bool]]                   # (agent_id) -> bool


def shape_observation(obs: dict[str, Any]) -> dict[str, Any]:
    """Map a `BrowserService.observe/snapshot` payload onto the runtime's execute-result keys.

    The runtime consumes ``url/title/dom/screenshot``; ``dom`` prefers raw HTML (present when the aria
    tree was thin) and otherwise the accessibility snapshot. The screenshot stays base64 here — the
    runtime stores it as a content-addressed artifact and never puts it in an event (design §10.6)."""
    return {
        "url": obs.get("url"),
        "title": obs.get("title"),
        "dom": obs.get("html") or obs.get("snapshot"),
        "screenshot": obs.get("screenshot_b64"),
    }


class ManagerBrowserDriver:
    def __init__(
        self,
        *,
        model: str = "",
        cwd: str | None = None,
        initializer: Initializer | None = None,
        service_provider: ServiceProvider | None = None,
        verifier: Verifier | None = None,
        closer: Closer | None = None,
    ) -> None:
        self._model = model
        self._cwd = cwd or os.getcwd()
        self._initializer = initializer
        self._service_provider = service_provider
        self._verifier = verifier
        self._closer = closer
        self._agent_by_profile: dict[str, str] = {}

    # --- BrowserDriver protocol -----------------------------------------------------------------

    async def launch(self, spec: DriverSpec) -> None:
        self._agent_by_profile[spec.profile_key] = spec.agent_id
        (self._initializer or self._real_init)(spec, self._model, self._cwd)

    async def execute(self, profile_key: str, intent: BrowserIntent) -> dict[str, Any]:
        agent_id = self._agent_for(profile_key)
        service = await self._service(agent_id)
        action, params = intent.action, intent.params
        if action in ("navigate", "goto"):
            obs = await service.navigate(params.get("url", ""), action="goto")
        elif action in ("back", "forward", "reload"):
            obs = await service.navigate(params.get("url", ""), action=action)
        else:  # snapshot / observe / anything else → observe the current page
            obs = await service.snapshot(include_screenshot=bool(params.get("screenshot", False)))
        return shape_observation(obs)

    async def verify_identity(self, profile_key: str) -> bool:
        agent_id = self._agent_for(profile_key)
        if self._verifier is not None:
            return self._verifier(agent_id)
        from tabvis.browser.manager import get_browser_service

        return get_browser_service(agent_id) is not None

    async def close(self, profile_key: str) -> None:
        agent_id = self._agent_by_profile.pop(profile_key, None)
        if agent_id is None:
            return
        if self._closer is not None:
            await self._closer(agent_id)
            return
        from tabvis.browser.manager import quit_agent_browser

        await quit_agent_browser(agent_id)

    # --- real-manager defaults ------------------------------------------------------------------

    @staticmethod
    def _real_init(spec: DriverSpec, model: str, cwd: str) -> None:
        from tabvis.browser.manager import init_browser_session

        init_browser_session(
            session_id=spec.session_id, model=model, cwd=cwd, agent_id=spec.agent_id, profile=spec.profile,
        )

    async def _service(self, agent_id: str) -> Any:
        if self._service_provider is not None:
            return await self._service_provider(agent_id)
        # Bind the agent so the manager resolves ITS workspace, create/return the service, then unbind —
        # the service object then drives its own pages without the ContextVar.
        from tabvis.browser.manager import bind_agent, get_or_create_browser_service, unbind_agent

        token = bind_agent(agent_id)
        try:
            return await get_or_create_browser_service()
        finally:
            unbind_agent(token)

    def _agent_for(self, profile_key: str) -> str:
        agent_id = self._agent_by_profile.get(profile_key)
        if agent_id is None:
            raise GatewayError("BROWSER_BINDING_NOT_FOUND", details={"profile_key": profile_key})
        return agent_id
