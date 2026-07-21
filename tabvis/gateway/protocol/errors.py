"""Stable error catalog (design §9.7, §15 Phase 0).

The protocol's contract is: **error codes are stable identifiers; human-readable messages are not.**
A client branches on ``error.code`` and never parses ``error.message``. Each code is registered once
here with its HTTP status and whether the operation is retryable, so the wire body and the HTTP layer
stay consistent no matter which handler raised it.

The error body shape is exactly the design's §9.7::

    {"error": {"code", "message", "retryable", "details", "trace_id"}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final


@dataclass(frozen=True)
class ErrorSpec:
    """The stable facts about one error code."""

    code: str
    http_status: int
    retryable: bool
    message: str  # default human message; a raiser may override the text but never the code


# --- the catalog -------------------------------------------------------------------------------
# One entry per stable code. Grouped by concern; keep additions append-only.

_SPECS: Final[tuple[ErrorSpec, ...]] = (
    # request / auth
    ErrorSpec("VALIDATION_FAILED", 400, False, "The command failed validation"),
    ErrorSpec("UNAUTHENTICATED", 401, False, "Authentication is required"),
    ErrorSpec("FORBIDDEN", 403, False, "The principal is not permitted to perform this action"),
    ErrorSpec("NOT_FOUND", 404, False, "The requested resource does not exist"),
    ErrorSpec("CONFLICT", 409, False, "The resource is in a conflicting state"),
    ErrorSpec("UNSUPPORTED_PROTOCOL", 400, False, "Unsupported protocol version"),
    ErrorSpec("UNKNOWN_COMMAND", 400, False, "Unknown command type"),
    # runs
    ErrorSpec("RUN_NOT_FOUND", 404, False, "No run with that id"),
    ErrorSpec("RUN_ALREADY_ACTIVE", 409, False, "Agent already has an active run"),
    ErrorSpec("RUN_NOT_ACTIVE", 409, False, "The run is not in an active state"),
    ErrorSpec("RUN_TERMINAL", 409, False, "The run has already reached a terminal state"),
    ErrorSpec("INVALID_STATE_TRANSITION", 409, False, "The requested run transition is not allowed"),
    ErrorSpec("CAPACITY_EXCEEDED", 429, True, "The gateway is at capacity; retry later"),
    # sessions
    ErrorSpec("SESSION_NOT_FOUND", 404, False, "No session with that id"),
    ErrorSpec("SESSION_FAILED", 409, False, "The session is in a failed state"),
    # interactions
    ErrorSpec("INTERACTION_NOT_FOUND", 404, False, "No pending interaction with that id"),
    ErrorSpec("INTERACTION_ALREADY_ANSWERED", 409, False, "The interaction was already answered"),
    ErrorSpec("INTERACTION_EXPIRED", 409, False, "The interaction expired before an answer arrived"),
    ErrorSpec("INTERACTION_CANCELLED", 409, False, "The interaction was cancelled before an answer arrived"),
    # browser runtime
    ErrorSpec("BROWSER_PROFILE_BUSY", 409, False, "The browser profile is in use by another run"),
    ErrorSpec("BROWSER_BINDING_NOT_FOUND", 404, False, "No browser binding with that id"),
    ErrorSpec("BROWSER_DISCONNECTED", 409, True, "The browser session is disconnected"),
    # infrastructure
    ErrorSpec("STORE_UNAVAILABLE", 503, True, "The metadata store is unavailable"),
    ErrorSpec("INTERNAL", 500, False, "An internal error occurred"),
)

CATALOG: Final[dict[str, ErrorSpec]] = {spec.code: spec for spec in _SPECS}


def spec_for(code: str) -> ErrorSpec:
    """The registered :class:`ErrorSpec`, or the INTERNAL spec for an unregistered code."""
    return CATALOG.get(code, CATALOG["INTERNAL"])


class GatewayError(Exception):
    """A protocol error carrying a stable code (design §9.7).

    Raise with a catalog ``code``; the HTTP status, retryable flag, and default message come from the
    catalog. Pass ``message`` only to add context to the human text — never to change the meaning,
    which the code alone conveys. ``details`` is a small JSON-safe dict for machine-usable specifics
    (e.g. ``{"run_id": ...}``); it MUST NOT carry secrets (design principle: secrets are references).
    """

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.spec = spec_for(code)
        self.code = self.spec.code
        self.message = message or self.spec.message
        self.details = details or {}
        self.trace_id = trace_id
        super().__init__(f"{self.code}: {self.message}")

    @property
    def http_status(self) -> int:
        return self.spec.http_status

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    def to_body(self) -> dict[str, Any]:
        """The §9.7 wire body: ``{"error": {...}}``."""
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
        if self.trace_id is not None:
            error["trace_id"] = self.trace_id
        return {"error": error}
