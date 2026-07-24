"""One-time credential capability issue & atomic consume (design §5.6, §7.2, §13.3).

A :class:`~tabvis.authentication.models.CredentialCapability` is the single-use token that authorizes
exactly one authentication. This store is the trusted-domain, **in-memory only** registry for them:

* capabilities exist only in process memory, so a process restart invalidates every outstanding one
  (design §13.3) — there is deliberately no disk persistence;
* :func:`consume` is atomic under a lock and single-use — a second consume of the same id fails
  (design §5.6, §16.4 "Capability 第二次使用必定失败");
* consuming validates expiry and that the live browser context (page id + navigation generation) still
  matches what was authorized (design §7.2) — any drift fails with ``page_changed``.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from tabvis.authentication.errors import AuthenticationError, AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext, CredentialCapability

# Default capability lifetime (design §5.6 "默认有效期 SHOULD 不超过 30 秒", §17 TTL knob default 30).
DEFAULT_CAPABILITY_TTL_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_capability_id() -> str:
    return "cap_" + uuid.uuid4().hex[:16]


class CapabilityStore:
    """In-memory, single-use capability registry. One instance lives in the trusted Broker process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, CredentialCapability] = {}

    def issue(
        self,
        *,
        credential_profile_id: str,
        context: BrowserAuthenticationContext,
        task_id: str,
        user_id: str,
        ttl_seconds: int = DEFAULT_CAPABILITY_TTL_SECONDS,
    ) -> CredentialCapability:
        """Mint a one-time capability bound to the live browser context (design §5.6)."""
        issued = _now()
        cap = CredentialCapability(
            id=new_capability_id(),
            credential_profile_id=credential_profile_id,
            browser_session_id=context.browser_session_id,
            task_id=task_id,
            user_id=user_id,
            top_level_origin=context.top_level_origin,
            frame_origin=context.frame_origin,
            page_id=context.page_id,
            navigation_generation=context.navigation_generation,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._by_id[cap.id] = cap
        return cap

    def consume(
        self, capability_id: str, *, context: BrowserAuthenticationContext
    ) -> CredentialCapability:
        """Atomically consume a capability, re-validating expiry and live context (design §7.2).

        The capability is removed from the registry *before* any validation so it can never be consumed
        twice, even if the context check then fails. Raises :class:`AuthenticationError` with a stable
        code on every failure path.
        """
        with self._lock:
            cap = self._by_id.pop(capability_id, None)
        if cap is None:
            # Either it never existed or it was already consumed — both surface as consumed so we never
            # leak whether a given id was valid.
            raise AuthenticationError(AuthErrorCode.CAPABILITY_CONSUMED)
        if _now() >= cap.expires_at:
            raise AuthenticationError(AuthErrorCode.CAPABILITY_EXPIRED)
        # Re-validate the live browser context against what was authorized. Any drift = re-authorize.
        if (
            context.browser_session_id != cap.browser_session_id
            or context.page_id != cap.page_id
            or context.navigation_generation != cap.navigation_generation
        ):
            raise AuthenticationError(AuthErrorCode.PAGE_CHANGED)
        if (
            context.top_level_origin != cap.top_level_origin
            or context.frame_origin != cap.frame_origin
        ):
            raise AuthenticationError(AuthErrorCode.ORIGIN_NOT_ALLOWED)
        return cap

    def invalidate(self, capability_id: str) -> None:
        """Drop a capability without consuming it (cancel / timeout / DLP block paths, design §13.2)."""
        with self._lock:
            self._by_id.pop(capability_id, None)

    def purge_expired(self) -> int:
        """Drop every expired capability; returns how many were removed (Broker startup sweep, §13.3)."""
        now = _now()
        with self._lock:
            expired = [cid for cid, cap in self._by_id.items() if now >= cap.expires_at]
            for cid in expired:
                self._by_id.pop(cid, None)
        return len(expired)

    def clear(self) -> None:
        """Drop everything (process shutdown / test reset)."""
        with self._lock:
            self._by_id.clear()
