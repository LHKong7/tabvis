"""Whitelist authentication audit events (design §12.1).

Auditing records *that* a credential was used, never its contents. This module is the single place an
authentication audit event is built, and it is a strict allowlist: the event model forbids extra
fields, so a caller cannot accidentally attach a username, cookie, URL query, DOM, selector or a raw
provider exception (design §12.1 禁止记录 list). If a value that looks secret-shaped is passed for an
allowlisted field, it is dropped rather than recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict


class CredentialAuditEvent(BaseModel):
    """A single, fully-redacted authentication audit record (design §12.1 example)."""

    model_config = ConfigDict(extra="forbid")

    event: str
    request_id: str
    credential_profile_id: str
    origin: str | None
    task_id: str
    user_id: str
    approved_by: str | None
    adapter: str
    success: bool
    error_code: str | None
    timestamp: str


def build_credential_used_event(
    *,
    request_id: str,
    credential_profile_id: str,
    origin: str | None,
    task_id: str,
    user_id: str,
    approved_by: str | None,
    adapter: str,
    success: bool,
    error_code: str | None = None,
    event: str = "credential_profile_used",
) -> CredentialAuditEvent:
    """Build the whitelisted audit event for a credential use.

    Only these fields are ever recorded (design §12.1). Anything not in this signature — the actual
    secret, the username, the cookie, the full URL, the DOM — has no path into the record because the
    model forbids extras and this builder has no parameter for them.
    """
    return CredentialAuditEvent(
        event=event,
        request_id=request_id,
        credential_profile_id=credential_profile_id,
        origin=origin,
        task_id=task_id,
        user_id=user_id,
        approved_by=approved_by,
        adapter=adapter,
        success=success,
        error_code=error_code,
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
