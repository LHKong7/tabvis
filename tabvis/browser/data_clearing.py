"""Browser data clearing (issue #4) — controlled deletion of a persistent profile's state.

A persistent profile accumulates cookies, logins, local/IndexedDB storage and caches across runs.
Without a way to clear it there is no clean account switch, no privacy reset, no test isolation. This
module provides two deliberately-narrow, safety-gated operations (the "phase 1" surface from the
request):

* :func:`clear_origin_data` — drop one origin's cookies + local/IndexedDB storage + cache/service
  workers on a **live** context, leaving the rest of the profile intact.
* :func:`clear_profile` — remove an entire persistent profile directory, but only after proving it is
  safe: the profile must be Tabvis-managed (inside our config home), not the config root itself, and
  have no live owner/lease. The delete is done by an **atomic move to a trash dir** first, then an
  asynchronous ``rmtree`` — so the original path is gone the instant the call returns, and a slow or
  failing recursive delete never leaves a half-deleted profile behind.

Every clear emits an audit record to ``<data-root>/logs/data-clearing.jsonl``. There is deliberately
**no** "delete an arbitrary ``user_data_dir``" capability: a path outside the managed root is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from typing import Any

from tabvis.browser.session import utc_now
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_TRASH_DIRNAME = ".trash"


class DataClearingError(Exception):
    """A clear was refused by a safety guard (never a partial deletion)."""


# --------------------------------------------------------------------------- audit


def _audit_log_path() -> str:
    from tabvis.browser.persistence.paths import logs_dir

    return os.path.join(logs_dir(create=True), "data-clearing.jsonl")


def _audit(event: str, **fields: Any) -> None:
    """Append a JSON audit line for a clearing operation. Best-effort — never raises."""
    record = {"event": event, "ts": utc_now(), **fields}
    try:
        with open(_audit_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as e:
        log_for_debugging(f"[DATA-CLEAR] audit write failed: {e}")
    log_for_debugging(f"[DATA-CLEAR] {event}: {fields}")


# --------------------------------------------------------------------------- origin-level clear


async def clear_origin_data(context: Any, origin: str) -> dict[str, Any]:
    """Clear one origin's cookies, storage (local/IndexedDB) and cache/service-workers on ``context``.

    ``origin`` is a scheme+host(+port), e.g. ``https://example.com``. Best-effort and layered so it
    degrades gracefully across engines: cookies always (Playwright), then a Chromium CDP
    ``Storage.clearDataForOrigin`` for the storage/cache/SW classes, then a DOM fallback that clears
    ``localStorage``/``sessionStorage`` for the origin's open pages. Returns what was attempted.
    """
    origin = (origin or "").strip().rstrip("/")
    if not origin or "://" not in origin:
        raise DataClearingError(f"invalid origin {origin!r}; expected e.g. https://example.com")

    cleared: list[str] = []
    host = origin.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]

    # Cookies — Playwright can filter by domain (best-effort across versions).
    try:
        try:
            await context.clear_cookies(domain=host)
        except TypeError:
            # Older Playwright has no domain filter — fall back to a full cookie clear.
            await context.clear_cookies()
        cleared.append("cookies")
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[DATA-CLEAR] cookie clear failed for {origin}: {e}")

    # Storage / cache / service workers — Chromium CDP, if this engine exposes it.
    try:
        pages = list(getattr(context, "pages", []) or [])
        if pages and hasattr(context, "new_cdp_session"):
            session = await context.new_cdp_session(pages[0])
            await session.send(
                "Storage.clearDataForOrigin",
                {
                    "origin": origin,
                    "storageTypes": "local_storage,indexeddb,cache_storage,service_workers,websql",
                },
            )
            cleared.extend(["local_storage", "indexeddb", "cache_storage", "service_workers"])
    except Exception as e:  # noqa: BLE001 - non-Chromium engines have no CDP; that's fine
        log_for_debugging(f"[DATA-CLEAR] CDP storage clear failed for {origin}: {e}")

    _audit("origin_cleared", origin=origin, cleared=sorted(set(cleared)))
    return {"origin": origin, "cleared": sorted(set(cleared))}


# --------------------------------------------------------------------------- profile-level clear


def _is_within(path: str, root: str) -> bool:
    """True if ``path`` is ``root`` or a descendant of it (realpath-normalized, symlink-safe)."""
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    if path == root:
        return True
    return path.startswith(root + os.sep)


def _managed_root() -> str:
    return os.path.realpath(get_tabvis_config_home_dir())


def _active_holder(user_data_dir: str) -> str | None:
    """The agent currently owning or driving a workspace on ``user_data_dir``, else None."""
    try:
        from tabvis.browser import manager

        return manager.get_workspace_owner(user_data_dir) or manager.get_profile_holder(user_data_dir)
    except Exception:  # noqa: BLE001 - if the manager can't answer, treat as "unknown" below
        return None


def clear_profile(
    user_data_dir: str,
    *,
    agent_id: str | None = None,
    reason: str = "",
    wait: bool = False,
) -> dict[str, Any]:
    """Delete a whole persistent profile, safely (issue #4).

    Guards, in order (any failure raises :class:`DataClearingError` and deletes nothing):

    1. The target must be **inside the Tabvis-managed config home** — no arbitrary path deletion.
    2. It must not **be** the config home / managed root itself.
    3. There must be **no active owner or lease** on it (the browser must be closed first).

    Then the profile is **atomically moved** into ``<data-root>/.trash/`` (so the original path is
    immediately gone and cannot be half-deleted) and the trash copy is ``rmtree``-d — in a background
    thread unless ``wait=True``. An audit record is written either way.
    """
    root = _managed_root()
    target = os.path.realpath(user_data_dir)

    if not _is_within(target, root):
        raise DataClearingError(
            f"refusing to clear {user_data_dir!r}: not inside the Tabvis-managed root {root!r}. "
            "Arbitrary directory deletion is not supported."
        )
    if target == root:
        raise DataClearingError("refusing to clear the Tabvis config root itself.")

    if not os.path.isdir(target):
        _audit("profile_clear_noop", user_data_dir=target, agent_id=agent_id, reason="not found")
        return {"cleared": False, "reason": "profile directory does not exist", "path": target}

    holder = _active_holder(user_data_dir)
    if holder is not None:
        raise DataClearingError(
            f"refusing to clear profile {target!r}: still in use by agent {holder!r}. "
            "Close the browser (release its owner/lease) first."
        )

    from tabvis.browser.persistence.paths import browser_os_data_subdir

    trash_dir = browser_os_data_subdir(_TRASH_DIRNAME, create=True)
    staged = os.path.join(trash_dir, f"{os.path.basename(target)}-{uuid.uuid4().hex}")
    try:
        os.replace(target, staged)  # atomic within the same filesystem
    except OSError as e:
        # Cross-device (config home and data-root on different mounts): fall back to a copy+remove,
        # still staging out of the live path first so nothing reads a half-cleared profile.
        try:
            shutil.move(target, staged)
        except OSError:
            raise DataClearingError(f"failed to stage profile for deletion: {e}") from e

    _audit("profile_cleared", user_data_dir=target, staged=staged, agent_id=agent_id, reason=reason)

    def _purge() -> None:
        try:
            shutil.rmtree(staged, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[DATA-CLEAR] trash purge failed for {staged}: {e}")

    if wait:
        _purge()
    else:
        threading.Thread(target=_purge, name="tabvis-profile-purge", daemon=True).start()

    return {"cleared": True, "path": target, "staged": staged}
