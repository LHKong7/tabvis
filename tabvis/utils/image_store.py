"""Per-session image store

Pasted/dragged images are written to ``<tabvis-config-home>/image-cache/<session-id>/<id>.<ext>``
so attachments survive across turns without re-pasting the base64. An in-memory id→path map
caches the most-recent paths (capped at :data:`MAX_STORED_IMAGE_PATHS`, evicting oldest first),
and stale per-session cache dirs from previous runs are pruned on demand.

Casing: Python identifiers are snake_case; ``PastedContent`` keeps its camelCase wire keys
(``mediaType`` etc.) since it round-trips through the structured-history config.

Faithful-behavior notes:
- ``open(path, 'w', 0o600).writeFile(content, {encoding:'base64'})`` → base64-decode the content
  to bytes and write them with ``0o600`` perms; ``datasync`` → :func:`os.fsync` on the fd.
- The in-memory ``Map<number, string>`` → an ``OrderedDict``-backed plain ``dict`` (Python dicts
  preserve insertion order), so ``evict_oldest_if_at_cap`` removes the first-inserted key like
  the TS ``keys().next().value``.
- ``getFsImplementation().readdir(baseDir)`` → ``await get_fs_implementation().readdir(...)``
  (returns ``Dirent`` objects with ``.name``); ``.rm``/``.rmdir`` match the TS calls.

``PastedContent`` is defined locally in this module as a ``TypedDict``; there is no
``tabvis.utils.config`` type to import it from in this build.
"""

from __future__ import annotations

import base64
import os
import os.path as _osp
from typing import TypedDict

from tabvis.bootstrap.state import get_session_id
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.fs_operations import get_fs_implementation


class PastedContent(TypedDict, total=False):
    """A pasted text/image entry (wire keys verbatim; mirror of ``config.ts`` ``PastedContent``)."""

    id: int  # Sequential numeric ID.
    type: str  # 'text' | 'image'.
    content: str  # Text, or base64-encoded image bytes.
    mediaType: str  # e.g. 'image/png', 'image/jpeg'.
    filename: str
    dimensions: object
    sourcePath: str


IMAGE_STORE_DIR = "image-cache"
MAX_STORED_IMAGE_PATHS = 200

# In-memory cache of stored image paths (insertion-ordered, oldest-first eviction).
_stored_image_paths: dict[int, str] = {}


def _get_image_store_dir() -> str:
    """Return the image store directory for the current session."""
    return _osp.join(get_tabvis_config_home_dir(), IMAGE_STORE_DIR, get_session_id())


async def _ensure_image_store_dir() -> None:
    """Ensure the image store directory exists."""
    await get_fs_implementation().mkdir(_get_image_store_dir(), {"recursive": True})


def _get_image_path(image_id: int, media_type: str) -> str:
    """Return the file path for an image by ID."""
    parts = media_type.split("/")
    extension = parts[1] if len(parts) > 1 and parts[1] else "png"
    return _osp.join(_get_image_store_dir(), f"{image_id}.{extension}")


def cache_image_path(content: PastedContent) -> str | None:
    """Cache the image path immediately (fast, no file I/O). Returns the path or ``None``."""
    if content.get("type") != "image":
        return None
    image_path = _get_image_path(content["id"], content.get("mediaType") or "image/png")
    _evict_oldest_if_at_cap()
    _stored_image_paths[content["id"]] = image_path
    return image_path


async def store_image(content: PastedContent) -> str | None:
    """Store an image from ``pastedContents`` to disk. Returns the path or ``None``."""
    if content.get("type") != "image":
        return None

    try:
        await _ensure_image_store_dir()
        image_path = _get_image_path(
            content["id"], content.get("mediaType") or "image/png"
        )
        raw = base64.b64decode(content.get("content") or "")
        fd = os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        _evict_oldest_if_at_cap()
        _stored_image_paths[content["id"]] = image_path
        log_for_debugging(f"Stored image {content['id']} to {image_path}")
        return image_path
    except Exception as error:  # noqa: BLE001 - TS catch-all → debug log + None
        log_for_debugging(f"Failed to store image: {error}")
        return None


async def store_images(
    pasted_contents: dict[int, PastedContent],
) -> dict[int, str]:
    """Store all images from ``pasted_contents`` to disk. Returns an id→path map."""
    path_map: dict[int, str] = {}

    for image_id, content in pasted_contents.items():
        if content.get("type") == "image":
            path = await store_image(content)
            if path:
                path_map[int(image_id)] = path

    return path_map


def get_stored_image_path(image_id: int) -> str | None:
    """Return the file path for a stored image by ID, or ``None``."""
    return _stored_image_paths.get(image_id)


def clear_stored_image_paths() -> None:
    """Clear the in-memory cache of stored image paths."""
    _stored_image_paths.clear()


def _evict_oldest_if_at_cap() -> None:
    while len(_stored_image_paths) >= MAX_STORED_IMAGE_PATHS:
        # dict preserves insertion order → first key is the oldest (TS keys().next().value).
        oldest = next(iter(_stored_image_paths), None)
        if oldest is not None:
            del _stored_image_paths[oldest]
        else:
            break


async def cleanup_old_image_caches() -> None:
    """Clean up old image cache directories from previous sessions."""
    fs_impl = get_fs_implementation()
    base_dir = _osp.join(get_tabvis_config_home_dir(), IMAGE_STORE_DIR)
    current_session_id = get_session_id()

    try:
        try:
            session_dirs = await fs_impl.readdir(base_dir)
        except OSError:
            return

        for session_dir in session_dirs:
            if session_dir.name == current_session_id:
                continue

            session_path = _osp.join(base_dir, session_dir.name)
            try:
                await fs_impl.rm(session_path, {"recursive": True, "force": True})
                log_for_debugging(f"Cleaned up old image cache: {session_path}")
            except OSError:
                # Ignore errors for individual directories.
                pass

        try:
            remaining = await fs_impl.readdir(base_dir)
            if len(remaining) == 0:
                await fs_impl.rmdir(base_dir)
        except OSError:
            # Ignore.
            pass
    except OSError:
        # Ignore errors reading the base directory.
        pass
