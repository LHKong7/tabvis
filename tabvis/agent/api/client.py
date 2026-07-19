"""Provider (Anthropic) client construction.

``get_provider_client`` builds an :class:`anthropic.AsyncAnthropic` configured for Tabvis local
mode. ``TABVIS_BASE_URL`` is REQUIRED — a clear error is raised if it is unset rather than falling
back to the public model endpoint. ``Authorization: Bearer <token>`` is sent as a default header
AND ``api_key``/``auth_token``/``base_url`` are passed to the SDK constructor. ``max_retries`` is
passed to the SDK, but retries are actually driven via ``with_retry`` (callers pass
``max_retries=0``).

Not supported in this build: proxy/mTLS fetch options, Foundry/Azure, an
``x-client-request-id`` injecting fetch wrapper, and a development provider request log.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

from anthropic import AsyncAnthropic

from tabvis.bootstrap.state import (
    get_is_non_interactive_session as _bootstrap_get_is_non_interactive_session,
)
from tabvis.bootstrap_macro import MACRO
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy

# Default request timeout (ms), used when API_TIMEOUT_MS is unset or invalid.
_DEFAULT_API_TIMEOUT_MS = 600 * 1000

# Per-request header injected for first-party base URLs so client-side timeouts (which carry no
# server request id) can still be correlated.
CLIENT_REQUEST_ID_HEADER = "x-client-request-id"


# --- tabvisEnv accessors -----------------------------------------------------------------------
# Only TABVIS_* names are accepted; the three readers used here are inlined.


def _get_tabvis_env(name: str) -> str | None:
    if not name.startswith("TABVIS_"):
        raise ValueError(f"get_tabvis_env only accepts TABVIS_* names, got {name}")
    return os.environ.get(name)


def get_tabvis_api_key() -> str | None:
    return _get_tabvis_env("TABVIS_API_KEY")


def get_tabvis_auth_token() -> str | None:
    return _get_tabvis_env("TABVIS_AUTH_TOKEN")


def get_tabvis_base_url() -> str | None:
    return _get_tabvis_env("TABVIS_BASE_URL")


_TABVIS_TO_PROVIDER_SDK_ENV: dict[str, str] = {
    "TABVIS_API_KEY": "ANTHROPIC_API_KEY",
    "TABVIS_AUTH_TOKEN": "ANTHROPIC_AUTH_TOKEN",
    "TABVIS_BASE_URL": "ANTHROPIC_BASE_URL",
    "TABVIS_FOUNDRY_API_KEY": "ANTHROPIC_FOUNDRY_API_KEY",
    "TABVIS_FOUNDRY_BASE_URL": "ANTHROPIC_FOUNDRY_BASE_URL",
    "TABVIS_FOUNDRY_RESOURCE": "ANTHROPIC_FOUNDRY_RESOURCE",
    "TABVIS_LOG": "ANTHROPIC_LOG",
}


def apply_provider_sdk_env_adapter() -> None:
    """Mirror TABVIS_* config onto the provider-SDK env names."""
    for tabvis_name, sdk_name in _TABVIS_TO_PROVIDER_SDK_ENV.items():
        tabvis_value = _get_tabvis_env(tabvis_name)
        if tabvis_value is not None:
            os.environ[sdk_name] = tabvis_value
        else:
            os.environ.pop(sdk_name, None)


# --- bootstrap/state stubs --------------------------------------------------------------------

_SESSION_ID: str = str(uuid.uuid4())


def get_session_id() -> str:
    return _SESSION_ID


def get_is_non_interactive_session() -> bool:
    # Delegates to the bootstrap-state singleton; headless ``-p`` runs are non-interactive.
    return _bootstrap_get_is_non_interactive_session()


# --- auth stub (getModelApiKey / getApiKeyFromApiKeyHelper) ----------------------------------


def get_model_api_key() -> str | None:
    return get_tabvis_api_key()


async def get_api_key_from_api_key_helper(is_non_interactive_session: bool) -> str | None:
    return None


# --- http stub (getUserAgent) -----------------------------------------------------------------


def get_user_agent() -> str:
    # WARNING: logging relies on ``tabvis-cli`` in the User-Agent — do not change casually.
    agent_sdk_version = os.environ.get("TABVIS_AGENT_SDK_VERSION")
    agent_sdk = f", agent-sdk/{agent_sdk_version}" if agent_sdk_version else ""
    client_app_env = os.environ.get("TABVIS_AGENT_SDK_CLIENT_APP")
    client_app = f", client-app/{client_app_env}" if client_app_env else ""
    user_type = os.environ.get("USER_TYPE", "")
    entrypoint = os.environ.get("TABVIS_ENTRYPOINT", "cli")
    return f"tabvis-cli/{MACRO.VERSION} ({user_type}, {entrypoint}{agent_sdk}{client_app})"


# --- custom headers ---------------------------------------------------------------------------


def _get_custom_headers() -> dict[str, str]:
    custom_headers: dict[str, str] = {}
    custom_headers_env = os.environ.get("TABVIS_CUSTOM_HEADERS")
    if not custom_headers_env:
        return custom_headers

    # Split by newlines to support multiple headers (\n or \r\n).
    header_strings = custom_headers_env.replace("\r\n", "\n").split("\n")
    for header_string in header_strings:
        if not header_string.strip():
            continue
        # Parse "Name: Value" (curl style); split on the first colon, then trim.
        colon_idx = header_string.find(":")
        if colon_idx == -1:
            continue
        name = header_string[:colon_idx].strip()
        value = header_string[colon_idx + 1 :].strip()
        if name:
            custom_headers[name] = value
    return custom_headers


async def _configure_api_key_headers(
    headers: dict[str, str], is_non_interactive_session: bool
) -> None:
    token = os.environ.get("TABVIS_AUTH_TOKEN") or await get_api_key_from_api_key_helper(
        is_non_interactive_session
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"


async def get_provider_client(
    *,
    api_key: str | None = None,
    max_retries: int,
    model: str | None = None,
    fetch_override: Callable[..., Any] | None = None,
    source: str | None = None,
) -> AsyncAnthropic:
    """Construct an :class:`AsyncAnthropic` for Tabvis local mode.

    Raises ``RuntimeError`` if ``TABVIS_BASE_URL`` is unset (refusing the default model endpoint).
    """
    apply_provider_sdk_env_adapter()

    container_id = os.environ.get("TABVIS_CONTAINER_ID")
    remote_session_id = os.environ.get("TABVIS_REMOTE_SESSION_ID")
    client_app = os.environ.get("TABVIS_AGENT_SDK_CLIENT_APP")
    custom_headers = _get_custom_headers()

    default_headers: dict[str, str] = {
        "x-app": "cli",
        "User-Agent": get_user_agent(),
        "X-Tabvis-Session-Id": get_session_id(),
        **custom_headers,
    }
    if container_id:
        default_headers["x-tabvis-remote-container-id"] = container_id
    if remote_session_id:
        default_headers["x-tabvis-remote-session-id"] = remote_session_id
    # SDK consumers can identify their app/library for backend analytics.
    if client_app:
        default_headers["x-client-app"] = client_app

    log_for_debugging(
        f"[API:request] Creating client, TABVIS_CUSTOM_HEADERS present: "
        f"{bool(os.environ.get('TABVIS_CUSTOM_HEADERS'))}, has Authorization header: "
        f"{'Authorization' in custom_headers}"
    )

    # Add additional protection header if enabled via env var.
    if is_env_truthy(os.environ.get("TABVIS_ADDITIONAL_PROTECTION")):
        default_headers["x-anthropic-additional-protection"] = "true"

    await _configure_api_key_headers(default_headers, get_is_non_interactive_session())

    # Determine authentication method based on available tokens.
    if not get_tabvis_base_url():
        raise RuntimeError(
            "TABVIS_BASE_URL is required for Tabvis local mode. Refusing to use the default "
            "model API endpoint."
        )

    try:
        timeout_s = int(os.environ.get("API_TIMEOUT_MS") or _DEFAULT_API_TIMEOUT_MS) / 1000
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_API_TIMEOUT_MS / 1000

    return AsyncAnthropic(
        api_key=api_key or get_model_api_key(),
        auth_token=get_tabvis_auth_token(),
        base_url=get_tabvis_base_url(),
        max_retries=max_retries,
        timeout=timeout_s,
        default_headers=default_headers,
    )
