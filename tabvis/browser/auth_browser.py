"""Restricted authentication browser for the Executor (design §6.4, §7.1, §8.2).

:class:`RestrictedAuthenticationBrowser` is the concrete :class:`AuthenticationBrowser` the Executor
drives. It wraps a low-level :class:`PageController` (the real one is Playwright-backed inside the
Browser Host; tests use a fake) and exposes *only* the safe surface of §6.4:

* it can locate fields, type a :class:`SecretValue`, activate/clear, inspect the redacted context and
  wait for a strong success signal;
* it exposes **no** ``cookies()`` / ``storage_state()`` / ``evaluate()`` / screenshot / DOM path — the
  ``PageController`` protocol simply has no such method, so an adapter cannot reach them (§6.3).

``type_secret`` is the one internal path a secret enters the browser (§6.4). Before every secret input
it re-validates the live page against the context snapshot taken when the browser was bound; any drift
in page id, navigation generation or origin aborts with ``page_changed`` (§7.1 step 13, §8.3).
"""

from __future__ import annotations

from typing import Protocol

from tabvis.authentication.adapters.base import (
    AuthenticationFieldHandle,
    AuthenticationFieldHints,
    AuthenticationSuccessCondition,
    FieldRole,
)
from tabvis.authentication.errors import AuthenticationError, AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext
from tabvis.authentication.secrets import SecretValue


class PageController(Protocol):
    """Low-level page primitives the Browser Host implements (Playwright-backed in production).

    Deliberately minimal and safe-by-omission: there is no method that returns a field value, cookie
    jar, storage, DOM or screenshot. ``type_bytes`` takes raw bytes so a secret never becomes a ``str``.
    """

    async def current_context(self) -> BrowserAuthenticationContext: ...

    async def find_field(self, role: FieldRole, hints: AuthenticationFieldHints) -> str | None: ...

    async def type_bytes(self, handle_id: str, data: bytes) -> None: ...

    async def activate(self, handle_id: str) -> None: ...

    async def clear_fields(self) -> None: ...

    async def check_signal(self, condition: AuthenticationSuccessCondition) -> bool: ...


class RestrictedAuthenticationBrowser:
    def __init__(self, controller: PageController, bound_context: BrowserAuthenticationContext) -> None:
        self._c = controller
        # Snapshot taken when the capability was authorized; every secret input is re-checked against it.
        self._bound = bound_context

    async def inspect_context(self) -> BrowserAuthenticationContext:
        return await self._c.current_context()

    async def locate_authentication_field(
        self, role: FieldRole, hints: AuthenticationFieldHints
    ) -> AuthenticationFieldHandle | None:
        handle_id = await self._c.find_field(role, hints)
        if handle_id is None:
            return None
        return AuthenticationFieldHandle(handle_id=handle_id, role=role)

    async def type_secret(self, field: AuthenticationFieldHandle, value: SecretValue) -> None:
        # Re-validate the live page against the bound snapshot BEFORE any secret enters (§7.1 step 13).
        await self._revalidate()
        view = value.borrow_bytes()
        try:
            await self._c.type_bytes(field.handle_id, bytes(view))
        finally:
            del view

    async def activate(self, field: AuthenticationFieldHandle) -> None:
        await self._c.activate(field.handle_id)

    async def clear_authentication_fields(self) -> None:
        await self._c.clear_fields()

    async def wait_for_authentication_signal(
        self, condition: AuthenticationSuccessCondition
    ) -> bool:
        return await self._c.check_signal(condition)

    async def _revalidate(self) -> None:
        live = await self._c.current_context()
        if (
            live.page_id != self._bound.page_id
            or live.navigation_generation != self._bound.navigation_generation
        ):
            raise AuthenticationError(AuthErrorCode.PAGE_CHANGED)
        if not (live.is_https and live.certificate_valid):
            raise AuthenticationError(AuthErrorCode.HTTPS_REQUIRED)
        if live.top_level_origin != self._bound.top_level_origin or (
            live.frame_origin != self._bound.frame_origin
        ):
            raise AuthenticationError(AuthErrorCode.ORIGIN_NOT_ALLOWED)
