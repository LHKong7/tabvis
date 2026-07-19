"""Session-log transport over the session-ingress HTTP host.

The auth/transport helper that persists and hydrates the session transcript against a remote
session-ingress endpoint, using the bearer token resolved by
:mod:`tabvis.utils.session_ingress_auth`. Two public verbs:

- :func:`append_session_log` — ``PUT`` a single :class:`~tabvis.types.logs.TranscriptMessage`
  using optimistic concurrency control (the ``Last-Uuid`` header). Per-session writes are
  serialized through :func:`tabvis.utils.sequential.sequential` so appends never interleave and
  race the append-chain head. Retries transient errors (network, 5xx, 429) with exponential
  backoff; on ``409`` adopts the server's last UUID (from the ``x-last-uuid`` response header, or
  by re-fetching the session) and retries; fails immediately on ``401``.
- :func:`get_session_logs` — ``GET`` the full transcript for hydration, validating the
  ``{loglines: [...]}`` envelope and seeding ``last_uuid_map`` from the chain head.

Plus :func:`clear_session` / :func:`clear_all_sessions` cache-eviction helpers.

Casing (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; module constants are
UPPER_CASE. The :class:`~tabvis.types.logs.TranscriptMessage` / :class:`~tabvis.types.logs.Entry`
payloads are transcript ENVELOPES — plain dicts/TypedDicts that round-trip to JSON, so their wire
keys (``uuid`` etc.) stay verbatim. The HTTP wire headers (``Authorization`` / ``Content-Type`` /
``Last-Uuid`` / ``x-last-uuid``) are kept verbatim too. The :data:`SessionIngressError` envelope
keeps its ``error.message`` / ``error.type`` wire shape.

HTTP behavior notes:
- Requests use ``httpx``, which does not raise on any status by default; a response status of 500
  or above is treated explicitly as the retryable-error branch, while statuses below 500 are
  inspected inline.
- Exponential backoff is computed as ``base_delay * 2 ** (attempt - 1)``, capped by :func:`min`.
- Finding the last UUID in a log list is done via a manual reverse scan
  (:func:`_find_last_uuid`).

Behavior notes:
- ``last_uuid_map`` and ``sequential_append_by_session`` are module-level caches;
  ``clear_session`` / ``clear_all_sessions`` evict them.
- :func:`tabvis.utils.sleep.sleep` takes **milliseconds**; the backoff delay is passed through
  unchanged.
- ``logForDiagnosticsNoPII`` event names are kept verbatim (no PII).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

import httpx

from tabvis.types.logs import Entry, TranscriptMessage
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.diag_logs import log_for_diagnostics_no_pii
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.log import log_error
from tabvis.utils.sequential import sequential
from tabvis.utils.session_ingress_auth import get_session_ingress_auth_token
from tabvis.utils.sleep import sleep
from tabvis.utils.slow_operations import json_stringify


class SessionIngressErrorBody(TypedDict, total=False):
    """The ``{message?, type?}`` body of a :data:`SessionIngressError`. Wire keys verbatim."""

    message: str
    type: str


class SessionIngressError(TypedDict, total=False):
    """The session-ingress error envelope (``response.data`` on a failed call). Wire keys verbatim."""

    error: SessionIngressErrorBody


MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_DELAY_MS = 8000
GET_TIMEOUT_MS = 20000

# Module-level state (per-session caches).
last_uuid_map: dict[str, str] = {}

# Per-session sequential wrappers to prevent concurrent log writes.
sequential_append_by_session: dict[
    str,
    Callable[[TranscriptMessage, str, dict[str, str]], Awaitable[bool]],
] = {}


def get_or_create_sequential_append(
    session_id: str,
) -> Callable[[TranscriptMessage, str, dict[str, str]], Awaitable[bool]]:
    """Get or create a sequential wrapper for a session.

    This ensures that log appends for a session are processed one at a time.
    """
    sequential_append = sequential_append_by_session.get(session_id)
    if sequential_append is None:

        async def append(
            entry: TranscriptMessage,
            url: str,
            headers: dict[str, str],
        ) -> bool:
            return await append_session_log_impl(session_id, entry, url, headers)

        sequential_append = sequential(append)
        sequential_append_by_session[session_id] = sequential_append
    return sequential_append


async def append_session_log_impl(
    session_id: str,
    entry: TranscriptMessage,
    url: str,
    headers: dict[str, str],
) -> bool:
    """Internal implementation of :func:`append_session_log` with retry logic.

    Retries on transient errors (network, 5xx, 429). On ``409``, adopts the server's last UUID and
    retries (handles stale state from a killed process's in-flight requests). Fails immediately on
    ``401``.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            last_uuid = last_uuid_map.get(session_id)
            request_headers = dict(headers)
            if last_uuid:
                request_headers["Last-Uuid"] = last_uuid

            async with httpx.AsyncClient() as client:
                response = await client.put(url, json=entry, headers=request_headers)

            # Treat 5xx responses as retryable errors by routing them into the catch path below.
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"server error {response.status_code}",
                    request=response.request,
                    response=response,
                )

            if response.status_code in (200, 201):
                last_uuid_map[session_id] = entry["uuid"]
                log_for_debugging(
                    f"Successfully persisted session log entry for session {session_id}"
                )
                return True

            if response.status_code == 409:
                # Check if our entry was actually stored (server returned 409 but entry exists).
                # This handles the scenario where the entry was stored but the client received an
                # error response, causing last_uuid_map to be stale.
                server_last_uuid = response.headers.get("x-last-uuid")
                if server_last_uuid == entry["uuid"]:
                    # Our entry IS the last entry on the server — it was stored successfully.
                    last_uuid_map[session_id] = entry["uuid"]
                    log_for_debugging(
                        f"Session entry {entry['uuid']} already present on server, "
                        "recovering from stale state"
                    )
                    log_for_diagnostics_no_pii(
                        "info", "session_persist_recovered_from_409"
                    )
                    return True

                # Another writer (e.g. an in-flight request from a killed process) advanced the
                # server's chain. Try to adopt the server's last UUID from the response header, or
                # re-fetch the session to discover it.
                if server_last_uuid:
                    last_uuid_map[session_id] = server_last_uuid
                    log_for_debugging(
                        f"Session 409: adopting server lastUuid={server_last_uuid} from header, "
                        f"retrying entry {entry['uuid']}"
                    )
                else:
                    # Server didn't return x-last-uuid (e.g. v1 endpoint). Re-fetch the session to
                    # discover the current head of the append chain.
                    logs = await fetch_session_logs_from_url(session_id, url, headers)
                    adopted_uuid = _find_last_uuid(logs)
                    if adopted_uuid:
                        last_uuid_map[session_id] = adopted_uuid
                        log_for_debugging(
                            f"Session 409: re-fetched {len(logs) if logs else 0} entries, "
                            f"adopting lastUuid={adopted_uuid}, retrying entry {entry['uuid']}"
                        )
                    else:
                        # Can't determine server state — give up.
                        error_data: SessionIngressError = _response_json(response)
                        error_message = (
                            (error_data.get("error") or {}).get("message")
                            or "Concurrent modification detected"
                        )
                        log_error(
                            Exception(
                                f"Session persistence conflict: UUID mismatch for session "
                                f"{session_id}, entry {entry['uuid']}. {error_message}"
                            )
                        )
                        log_for_diagnostics_no_pii(
                            "error", "session_persist_fail_concurrent_modification"
                        )
                        return False
                log_for_diagnostics_no_pii(
                    "info", "session_persist_409_adopt_server_uuid"
                )
                continue  # retry with the updated last_uuid

            if response.status_code == 401:
                log_for_debugging("Session token expired or invalid")
                log_for_diagnostics_no_pii("error", "session_persist_fail_bad_token")
                return False  # non-retryable

            # Other 4xx (429, etc.) — retryable.
            log_for_debugging(
                f"Failed to persist session log: {response.status_code} "
                f"{response.reason_phrase}"
            )
            log_for_diagnostics_no_pii(
                "error",
                "session_persist_fail_status",
                {"status": response.status_code, "attempt": attempt},
            )
        except Exception as error:  # noqa: BLE001 - broad catch-all for network errors, 5xx
            # Network errors, 5xx — retryable.
            status = getattr(getattr(error, "response", None), "status_code", None)
            log_error(Exception(f"Error persisting session log: {error}"))
            log_for_diagnostics_no_pii(
                "error",
                "session_persist_fail_status",
                {"status": status, "attempt": attempt},
            )

        if attempt == MAX_RETRIES:
            log_for_debugging(f"Remote persistence failed after {MAX_RETRIES} attempts")
            log_for_diagnostics_no_pii(
                "error",
                "session_persist_error_retries_exhausted",
                {"attempt": attempt},
            )
            return False

        delay_ms = min(BASE_DELAY_MS * (2 ** (attempt - 1)), MAX_DELAY_MS)
        log_for_debugging(
            f"Remote persistence attempt {attempt}/{MAX_RETRIES} failed, "
            f"retrying in {delay_ms}ms…"
        )
        await sleep(delay_ms)

    return False


async def append_session_log(
    session_id: str,
    entry: TranscriptMessage,
    url: str,
) -> bool:
    """Append a log entry to the session using the JWT token.

    Uses optimistic concurrency control with the ``Last-Uuid`` header. Ensures sequential
    execution per session to prevent race conditions.
    """
    session_token = get_session_ingress_auth_token()
    if not session_token:
        log_for_debugging("No session token available for session persistence")
        log_for_diagnostics_no_pii("error", "session_persist_fail_jwt_no_token")
        return False

    headers: dict[str, str] = {
        "Authorization": f"Bearer {session_token}",
        "Content-Type": "application/json",
    }

    sequential_append = get_or_create_sequential_append(session_id)
    return await sequential_append(entry, url, headers)


async def get_session_logs(
    session_id: str,
    url: str,
) -> list[Entry] | None:
    """Get all session logs for hydration."""
    session_token = get_session_ingress_auth_token()
    if not session_token:
        log_for_debugging("No session token available for fetching session logs")
        log_for_diagnostics_no_pii("error", "session_get_fail_no_token")
        return None

    headers = {"Authorization": f"Bearer {session_token}"}
    logs = await fetch_session_logs_from_url(session_id, url, headers)

    if logs and len(logs) > 0:
        # Update our last_uuid to the last entry's UUID.
        last_entry = logs[-1]
        if last_entry and "uuid" in last_entry and last_entry["uuid"]:
            last_uuid_map[session_id] = last_entry["uuid"]

    return logs


async def fetch_session_logs_from_url(
    session_id: str,
    url: str,
    headers: dict[str, str],
) -> list[Entry] | None:
    """Shared implementation for fetching session logs from a URL."""
    try:
        params = (
            {"after_last_compact": "true"}
            if is_env_truthy(os.environ.get("TABVIS_AFTER_LAST_COMPACT"))
            else None
        )
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=headers,
                timeout=GET_TIMEOUT_MS / 1000,
                params=params,
            )

        # Treat 5xx responses as retryable errors.
        if response.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"server error {response.status_code}",
                request=response.request,
                response=response,
            )

        if response.status_code == 200:
            data = _response_json(response)

            # Validate the response structure.
            if (
                not data
                or not isinstance(data, dict)
                or not isinstance(data.get("loglines"), list)
            ):
                log_error(
                    Exception(
                        f"Invalid session logs response format: {json_stringify(data)}"
                    )
                )
                log_for_diagnostics_no_pii(
                    "error", "session_get_fail_invalid_response"
                )
                return None

            logs: list[Entry] = data["loglines"]
            log_for_debugging(
                f"Fetched {len(logs)} session logs for session {session_id}"
            )
            return logs

        if response.status_code == 404:
            log_for_debugging(f"No existing logs for session {session_id}")
            log_for_diagnostics_no_pii("warn", "session_get_no_logs_for_session")
            return []

        if response.status_code == 401:
            log_for_debugging("Auth token expired or invalid")
            log_for_diagnostics_no_pii("error", "session_get_fail_bad_token")
            raise Exception(
                "Your session ingress token has expired. Restart the session with valid "
                "environment credentials."
            )

        log_for_debugging(
            f"Failed to fetch session logs: {response.status_code} "
            f"{response.reason_phrase}"
        )
        log_for_diagnostics_no_pii(
            "error",
            "session_get_fail_status",
            {"status": response.status_code},
        )
        return None
    except Exception as error:  # noqa: BLE001 - broad catch-all for network/parsing errors
        # The "token expired" raise above is a deliberate error that should propagate rather than
        # be swallowed here. Detect it by message identity so it re-raises cleanly.
        if isinstance(error, Exception) and "session ingress token has expired" in str(
            error
        ):
            raise
        status = getattr(getattr(error, "response", None), "status_code", None)
        log_error(Exception(f"Error fetching session logs: {error}"))
        log_for_diagnostics_no_pii(
            "error",
            "session_get_fail_status",
            {"status": status},
        )
        return None


def _find_last_uuid(logs: list[Entry] | None) -> str | None:
    """Walk backward through entries to find the last one with a uuid.

    Some entry types (SummaryMessage, TagMessage) don't have one.
    """
    if not logs:
        return None
    for entry in reversed(logs):
        if "uuid" in entry and entry["uuid"]:
            return entry["uuid"]
    return None


def _response_json(response: httpx.Response) -> Any:
    """Parse the response body as JSON, or ``{}`` on parse failure.

    A non-JSON body collapses to ``{}`` here, which callers treat as an absent/empty error
    envelope.
    """
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return {}


def clear_session(session_id: str) -> None:
    """Clear cached state for a session."""
    last_uuid_map.pop(session_id, None)
    sequential_append_by_session.pop(session_id, None)


def clear_all_sessions() -> None:
    """Clear all cached session state (all sessions).

    Use this on ``/clear`` to free sub-agent session entries.
    """
    last_uuid_map.clear()
    sequential_append_by_session.clear()
