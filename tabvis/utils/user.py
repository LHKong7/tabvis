"""Core user data for analytics providers

Builds the :class:`CoreUserData` envelope that all analytics providers (and GrowthBook) consume:
device id, session id, git email, app version, host platform, user type, and optional GitHub
Actions metadata. The email is pre-fetched asynchronously at startup (``init_user``) so that the
synchronous :func:`get_core_user_data` getter never blocks on the ``git config`` subprocess.

Casing (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case, but the
:class:`CoreUserData` / :class:`GitHubActionsMetadata` payloads round-trip to analytics
providers, so their dict keys keep the TS **camelCase** wire keys verbatim (``deviceId`` /
``sessionId`` / ``appVersion`` / ``userType`` / ``firstTokenTime`` / ``githubActionsMetadata`` /
``actorId`` / ``repositoryOwnerId`` …).

lodash ``memoize`` semantics (replicated faithfully):
- ``getCoreUserData`` is memoized on its first argument (``_include_analytics_metadata``); lodash
  keys the cache on the first arg, so ``get_core_user_data()`` (key ``None``) and
  ``get_core_user_data(True)`` are **distinct** cache entries. Exposes ``.cache.clear()``.
- ``getGitEmail`` is memoized over a zero-arg **async** function: the subprocess spawns once per
  process, the awaited result is cached, and ``.cache.clear()`` resets it. Implemented as a
  single-slot async memo (``functools.lru_cache`` can't wrap coroutines).

Unported deps (classifier false-positives — minimal faithful local fallbacks, recorded in
``unported_dep_stubs``):
- ``getOrCreateUserID`` (``src/utils/config.ts``): the existing ``tabvis.utils.config`` only carries
  the ``enable_configs`` gate, not the global-config-backed device-id generator. Fallback: read /
  generate a 32-byte hex id persisted under the global ``.tabvis.json`` (matching the TS
  ``randomBytes(32).toString('hex')`` + ``saveGlobalConfig`` shape) with an in-memory cache.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import Callable
from typing import Any, TypedDict

from tabvis.bootstrap.state import get_session_id
from tabvis.bootstrap_macro import MACRO
from tabvis.utils.cwd import get_cwd
from tabvis.utils.env import get_host_platform_for_analytics
from tabvis.utils.env_utils import is_env_truthy

__all__ = [
    "CoreUserData",
    "GitHubActionsMetadata",
    "get_core_user_data",
    "get_git_email",
    "get_user_for_growth_book",
    "init_user",
    "reset_user_cache",
]


class GitHubActionsMetadata(TypedDict, total=False):
    """GitHub Actions metadata when running in CI (camelCase wire keys)."""

    actor: str
    actorId: str
    repository: str
    repositoryId: str
    repositoryOwner: str
    repositoryOwnerId: str


class CoreUserData(TypedDict, total=False):
    """Core user data used as base for all analytics providers.

    This is also the format used by GrowthBook. Keys are camelCase wire keys.
    """

    deviceId: str
    sessionId: str
    email: str | None
    appVersion: str
    platform: str
    userType: str | None
    firstTokenTime: float | None
    githubActionsMetadata: GitHubActionsMetadata


# --------------------------------------------------------------------------------------------
# lodash-memoize replicas (key on the 1st arg; expose .cache.clear()).
# --------------------------------------------------------------------------------------------


class _MemoCache:
    """Minimal lodash ``memoize.Cache`` shim — only ``clear`` is exercised by the callers."""

    def __init__(self) -> None:
        self._store: dict[Any, Any] = {}

    def clear(self) -> None:
        self._store.clear()

    def has(self, key: Any) -> bool:
        return key in self._store

    def get(self, key: Any) -> Any:
        return self._store[key]

    def set(self, key: Any, value: Any) -> None:
        self._store[key] = value


class _Memoized:
    """A lodash-``memoize``-compatible wrapper: caches on the first arg, exposes ``.cache``."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        self._fn = fn
        self.cache = _MemoCache()

    def __call__(self, *args: Any) -> Any:
        key = args[0] if args else None
        if self.cache.has(key):
            return self.cache.get(key)
        value = self._fn(*args)
        self.cache.set(key, value)
        return value


# --------------------------------------------------------------------------------------------
# Email pre-fetch state (module-level `let` parity).
# --------------------------------------------------------------------------------------------

# ``None`` means "not fetched yet" (the TS sentinel is ``null``); a string / absent value is the
# resolved email. We distinguish "not fetched" via a private sentinel object so a successfully
# resolved ``None`` email (git not configured) is still treated as fetched.
_NOT_FETCHED = object()
_cached_email: Any = _NOT_FETCHED
_email_fetch_task: asyncio.Task[str | None] | None = None


# --------------------------------------------------------------------------------------------
# getOrCreateUserID fallback (unported dep — see module docstring).
# --------------------------------------------------------------------------------------------


def _global_tabvis_file() -> str:
    """Path to the global ``.tabvis.json`` (``TABVIS_CONFIG_DIR`` or home), matching ``env.get_global_tabvis_file``."""
    base = os.environ.get("TABVIS_CONFIG_DIR") or os.path.expanduser("~")
    return os.path.join(base, ".tabvis.json")


def _get_or_create_user_id() -> str:
    """Return the or create user id.

    Reads ``userID`` from the global config; if absent, generates ``randomBytes(32).toString('hex')``
    and persists it. Mirrors the TS shape without pulling in the full config subsystem.
    """
    path = _global_tabvis_file()
    config: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                config = loaded
    except (OSError, ValueError):
        config = {}

    existing = config.get("userID")
    if isinstance(existing, str) and existing:
        return existing

    user_id = secrets.token_hex(32)
    config["userID"] = user_id
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
    except OSError:
        # Persistence is best-effort; the generated id is still returned for this process.
        pass
    return user_id


# --------------------------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------------------------


async def init_user() -> None:
    """Initialize user data asynchronously. Should be called early in startup.

    Pre-fetches the git email so :func:`get_core_user_data` can stay synchronous. Idempotent: once
    the email is fetched it short-circuits.
    """
    global _cached_email, _email_fetch_task
    if _cached_email is _NOT_FETCHED and _email_fetch_task is None:
        _email_fetch_task = asyncio.ensure_future(_get_email_async())
        _cached_email = await _email_fetch_task
        _email_fetch_task = None
        # Clear memoization cache so the next call picks up the email.
        get_core_user_data.cache.clear()


def reset_user_cache() -> None:
    """Reset all user data caches.

    Call on auth changes (login/logout/account switch) so the next :func:`get_core_user_data`
    picks up fresh credentials and email.
    """
    global _cached_email, _email_fetch_task
    _cached_email = _NOT_FETCHED
    _email_fetch_task = None
    get_core_user_data.cache.clear()
    get_git_email.cache.clear()


def _get_core_user_data_impl(_include_analytics_metadata: bool | None = None) -> CoreUserData:
    device_id = _get_or_create_user_id()
    # ``firstTokenTime`` is declared-but-unassigned in the TS source (always undefined here).
    first_token_time: float | None = None

    data: CoreUserData = {
        "deviceId": device_id,
        "sessionId": str(get_session_id()),
        "email": _get_email(),
        "appVersion": MACRO.VERSION,
        "platform": get_host_platform_for_analytics(),
        "userType": os.environ.get("USER_TYPE"),
        "firstTokenTime": first_token_time,
    }
    if is_env_truthy(os.environ.get("GITHUB_ACTIONS")):
        data["githubActionsMetadata"] = {
            "actor": os.environ.get("GITHUB_ACTOR"),  # type: ignore[typeddict-item]
            "actorId": os.environ.get("GITHUB_ACTOR_ID"),  # type: ignore[typeddict-item]
            "repository": os.environ.get("GITHUB_REPOSITORY"),  # type: ignore[typeddict-item]
            "repositoryId": os.environ.get("GITHUB_REPOSITORY_ID"),  # type: ignore[typeddict-item]
            "repositoryOwner": os.environ.get("GITHUB_REPOSITORY_OWNER"),  # type: ignore[typeddict-item]
            "repositoryOwnerId": os.environ.get(  # type: ignore[typeddict-item]
                "GITHUB_REPOSITORY_OWNER_ID"
            ),
        }
    return data


# Get core user data. The base representation that gets transformed for different analytics
# providers. Memoized on the first arg (lodash semantics): distinct entries per arg value.
get_core_user_data = _Memoized(_get_core_user_data_impl)


def get_user_for_growth_book() -> CoreUserData:
    """Get user data for GrowthBook (same as core data with analytics metadata)."""
    return get_core_user_data(True)


def _get_email() -> str | None:
    """Return the cached email if it was fetched (from async init); else ``None``."""
    if _cached_email is not _NOT_FETCHED:
        return _cached_email
    return None


async def _get_email_async() -> str | None:
    return await get_git_email()


# --------------------------------------------------------------------------------------------
# get_git_email — single-slot async memo (lodash memoize over a zero-arg async fn).
# --------------------------------------------------------------------------------------------


class _AsyncZeroArgMemo:
    """lodash ``memoize`` over a zero-arg async fn: caches the resolved value; ``.cache.clear()``."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn
        self.cache = _MemoCache()

    async def __call__(self) -> Any:
        # lodash keys a zero-arg call on ``undefined``; one shared slot.
        if self.cache.has(None):
            return self.cache.get(None)
        value = await self._fn()
        self.cache.set(None, value)
        return value


async def _git_email_impl() -> str | None:
    """``git config --get user.email`` (shell), trimmed; ``None`` on failure or empty output."""
    proc = await asyncio.create_subprocess_shell(
        "git config --get user.email",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=get_cwd(),
    )
    stdout_b, _ = await proc.communicate()
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    if proc.returncode == 0 and stdout:
        trimmed = stdout.strip()
        return trimmed or None
    return None


# Memoized so the subprocess only spawns once per process.
get_git_email = _AsyncZeroArgMemo(_git_email_impl)
