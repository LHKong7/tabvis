"""BrowserAuthenticate — the Agent's ONLY authentication surface (design §5.1, §14).

The Agent asks to authenticate the current browser page *using a stored credential profile*, and gets
back only a redacted :class:`~tabvis.authentication.models.AuthenticationResult` (success / origin /
human-required / stable error code). Two hard contracts (design §14, §16.4):

* the input schema has exactly one field, ``credential_profile_id`` — there is no ``username`` /
  ``password`` / ``totp`` / ``secret_ref`` / ``cookie`` field, and ``extra="forbid"`` makes any attempt
  to add one a validation error;
* this module MUST NOT import ``secret_store`` or ``BrowserService``. The trusted context (task / user /
  session / origin) is injected by the Orchestrator, and the actual authentication is performed by the
  managed runtime through a narrow Broker client.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.agent.tools.browser_common import playwright_available
from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import AgentAuthenticationRequest, AuthenticationResult
from tabvis.constants.tools import BROWSER_AUTHENTICATE_TOOL_NAME
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.permissions import PermissionDecision
from tabvis.utils.env_utils import is_env_truthy

_DESCRIPTION = """Authenticate the current browser page using a stored credential profile.

You do NOT handle the account, password, or one-time code — you only name a credential profile by id,
and a trusted service performs the login for you behind a locked browser session.

Usage:
 - credential_profile_id: the id of a credential profile the user has configured (e.g. 'work_sso').
 - Navigate to the site's login page first, then call this tool.
 - You will get back only: whether it succeeded, the authenticated origin, whether a human must finish
   the login (CAPTCHA / hardware key / push MFA), and — on failure — a stable error code.
 - You cannot read the secrets, cookies, or session; there is no way to make this tool reveal them."""


class BrowserAuthenticateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_profile_id: str = Field(
        description="Id of the stored credential profile to authenticate with (not a secret)."
    )


def _authentication_enabled() -> bool:
    # Gated off by default (design §17 TABVIS_AUTHENTICATION_ENABLED).
    import os

    return is_env_truthy(os.environ.get("TABVIS_AUTHENTICATION_ENABLED"))


class BrowserAuthenticateTool(Tool):
    name = BROWSER_AUTHENTICATE_TOOL_NAME
    search_hint = "browser: log in / authenticate with a saved credential profile by id"
    input_schema = BrowserAuthenticateInput
    should_defer = False
    always_load = False  # gated; only surfaced when authentication is enabled
    max_result_size_chars = 4_000

    def is_enabled(self) -> bool:
        return playwright_available() and _authentication_enabled()

    def is_read_only(self, input: Any) -> bool:
        return False

    def is_concurrency_safe(self, input: Any) -> bool:
        # A login takes an exclusive authentication lease on the browser (design §13.1).
        return False

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return "Authenticate the browser with a saved credential profile"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserAuthenticate"

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        from tabvis.browser.policy_guard import evaluate

        return evaluate(self.name, input, context)

    async def prompt(self, options: dict[str, Any]) -> str:
        return _DESCRIPTION

    async def call(
        self,
        args: BrowserAuthenticateInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        service = context.authentication_service
        if service is None:
            result = _internal_failure()
            return ToolResult(data=result.model_dump())
        try:
            request = AgentAuthenticationRequest(
                credential_profile_id=args.credential_profile_id
            )
            result = await service.authenticate(request, context=context)
            # Re-validate the trust-boundary response against the strict Agent-visible schema.
            result = AuthenticationResult.model_validate(result)
        except Exception:  # noqa: BLE001 - never expose Broker/browser/provider exception details
            result = _internal_failure()
        return ToolResult(data=result.model_dump())

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        safe = AuthenticationResult.model_validate(content).model_dump()
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(safe, separators=(",", ":"), sort_keys=True),
            "is_error": not safe["success"],
        }


def _internal_failure() -> AuthenticationResult:
    return AuthenticationResult(
        success=False,
        error_code=AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR.value,
    )


browser_authenticate_tool = BrowserAuthenticateTool()
