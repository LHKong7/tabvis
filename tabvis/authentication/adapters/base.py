"""Restricted authentication browser + adapter contracts (design §6.3, §6.4, §9.4)."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from tabvis.authentication.models import BrowserAuthenticationContext
from tabvis.authentication.secrets import SecretValue

FieldRole = Literal["username", "password", "totp", "submit"]


class AuthenticationFieldHints(BaseModel):
    """Non-secret hints an adapter passes to locate a field (design §6.4).

    These are semantic locators (autocomplete tokens, input type, aria/label text) — never a
    model-generated CSS selector (design §9.2 "优先语义 Locator，不使用模型生成选择器").
    """

    model_config = ConfigDict(extra="forbid")

    autocomplete: str | None = None
    input_type: str | None = None
    label_contains: list[str] = []
    name_contains: list[str] = []


class AuthenticationFieldHandle(BaseModel):
    """An opaque handle to a located field, minted by the authentication browser (design §6.4).

    It deliberately carries no field value — an adapter can act on the field (type/activate/clear) but
    can never read what is in it (§6.3 "禁止返回输入字段值"). ``handle_id`` is meaningful only to the
    browser that issued it, and it is bound to the current authentication lease.
    """

    model_config = ConfigDict(extra="forbid")

    handle_id: str
    role: FieldRole


class AuthenticationSuccessCondition(BaseModel):
    """A strong success signal an adapter waits on (design §9.4).

    Success MUST NOT rest on "the URL changed" alone. A condition names one strong signal the browser
    can evaluate *without* returning secret content: a cookie's mere presence (boolean, never its
    value), a same-origin account-status API boolean, an authenticated DOM condition, or the login form
    disappearing + a user menu appearing.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["cookie_present", "dom_condition", "account_api_ok", "logged_in_ui"]
    # For cookie_present: the cookie name (the browser returns only a boolean for it).
    cookie_name: str | None = None
    # For dom_condition / logged_in_ui: a semantic marker id the site adapter declares (never a model
    # selector). For account_api_ok: a same-origin path the browser checks in-origin.
    marker: str | None = None
    timeout_seconds: float = 15.0


class AdapterAuthenticationResult(BaseModel):
    """What an adapter returns to the Executor (design §6.3).

    Strictly booleans + a stable error code + the (host-recomputed) authenticated origin. No exception
    text, selector, username, cookie or site body — same allowlist discipline as the Agent-facing
    result (§5.3, §6.3 "把异常参数返回 Broker" is forbidden).
    """

    model_config = ConfigDict(extra="forbid")

    success: bool
    authenticated_origin: str | None = None
    requires_human_interaction: bool = False
    error_code: str | None = None


@runtime_checkable
class AuthenticationBrowser(Protocol):
    """The only browser surface an adapter sees (design §6.4).

    Every method is safe-by-construction: it either acts on the page or returns a boolean / redacted
    context — none returns a field value, cookie jar, storage, DOM or screenshot.
    """

    async def inspect_context(self) -> BrowserAuthenticationContext: ...

    async def locate_authentication_field(
        self, role: FieldRole, hints: AuthenticationFieldHints
    ) -> AuthenticationFieldHandle | None: ...

    async def type_secret(
        self, field: AuthenticationFieldHandle, value: SecretValue
    ) -> None: ...

    async def activate(self, field: AuthenticationFieldHandle) -> None: ...

    async def clear_authentication_fields(self) -> None: ...

    async def wait_for_authentication_signal(
        self, condition: AuthenticationSuccessCondition
    ) -> bool: ...


class AuthenticationAdapter(Protocol):
    """A site (or generic) login driver (design §6.3).

    ``name`` MUST be version-suffixed (§9.1). ``authenticate`` receives the restricted browser, the
    profile and the resolved credentials, and returns a redacted :class:`AdapterAuthenticationResult`.
    """

    name: str

    async def authenticate(
        self,
        browser: AuthenticationBrowser,
        profile: "object",
        credentials: "object",
    ) -> AdapterAuthenticationResult: ...
