"""Session environment script loading

Loads shell-environment fragments persisted by hooks (and an optional ``TABVIS_ENV_FILE``) into
a single script that can be sourced before running shell commands, so venv/conda activation and
hook-set env survive across commands within a session.

Faithful-behavior notes:
- TS ``fs/promises`` (``mkdir``/``readdir``/``readFile``/``writeFile``) → stdlib ``os`` file ops
  wrapped in :func:`asyncio.to_thread` (house style — see ``fs_operations.py``). ``readFile(..,
  'utf8').trim()`` → ``read_text(encoding='utf-8').strip()``.
- ``ENOENT`` is swallowed (a missing dir/file is normal); any other errno is logged via
  :func:`log_for_debugging`. ``get_errno_code`` is reused from ``utils/errors``.
- The module-level cache ``_session_env_script`` reproduces the TS tri-state:
  ``_UNSET`` sentinel = not yet loaded (check disk), ``None`` = checked, no files, ``str`` =
  loaded. (Python can't use bare ``None`` for "no files" *and* "unset", so a sentinel object
  stands in for TS ``undefined``.)
- Hook-file ordering: filter on ``HOOK_ENV_REGEX`` then sort by (priority of type, numeric index)
  so the resulting env is deterministic — identical key function to ``sortHookEnvFiles``.
- Windows is explicitly unsupported (returns ``None``), matching the TS guard.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Literal

from tabvis.bootstrap.state import get_session_id
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.errors import get_errno_code, get_error_message
from tabvis.utils.platform import get_platform

HookEvent = Literal["Setup", "SessionStart", "CwdChanged", "FileChanged"]


# Sentinel for the TS ``undefined`` cache state (not yet loaded). ``None`` distinctly means
# "checked disk, no files exist". A plain object identity check matches TS ``=== undefined``.
class _Unset:
    pass


_UNSET = _Unset()

# Cache states: ``_UNSET`` = not yet loaded; ``None`` = no files; ``str`` = loaded.
_session_env_script: str | None | _Unset = _UNSET


async def get_session_env_dir_path() -> str:
    """Return (creating if needed) the per-session env directory under the config home."""
    session_env_dir = os.path.join(
        get_tabvis_config_home_dir(),
        "session-env",
        str(get_session_id()),
    )
    await asyncio.to_thread(os.makedirs, session_env_dir, exist_ok=True)
    return session_env_dir


async def get_hook_env_file_path(hook_event: HookEvent, hook_index: int) -> str:
    """Path of the ``<event>-hook-<index>.sh`` file for a given hook event/index."""
    prefix = hook_event.lower()
    return os.path.join(await get_session_env_dir_path(), f"{prefix}-hook-{hook_index}.sh")


async def clear_cwd_env_files() -> None:
    """Blank out cwd/file-changed hook env files (so stale cwd env doesn't leak)."""
    try:
        dir_path = await get_session_env_dir_path()
        files = await asyncio.to_thread(os.listdir, dir_path)
        targets = [
            f
            for f in files
            if (f.startswith("filechanged-hook-") or f.startswith("cwdchanged-hook-"))
            and HOOK_ENV_REGEX.match(f)
        ]

        def _truncate(name: str) -> None:
            Path(os.path.join(dir_path, name)).write_text("", encoding="utf-8")

        await asyncio.gather(*[asyncio.to_thread(_truncate, f) for f in targets])
    except Exception as e:  # noqa: BLE001
        code = get_errno_code(e)
        if code != "ENOENT":
            log_for_debugging(f"Failed to clear cwd env files: {get_error_message(e)}")


def invalidate_session_env_cache() -> None:
    """Drop the cached script so the next read re-scans disk."""
    global _session_env_script
    log_for_debugging("Invalidating session environment cache")
    _session_env_script = _UNSET


async def get_session_environment_script() -> str | None:
    """Build (and cache) the combined session env script, or ``None`` if there is none."""
    global _session_env_script

    if get_platform() == "windows":
        log_for_debugging("Session environment not yet supported on Windows")
        return None

    if not isinstance(_session_env_script, _Unset):
        return _session_env_script

    scripts: list[str] = []

    # Check for TABVIS_ENV_FILE passed from parent process (e.g. HFI trajectory runner).
    # This allows venv/conda activation to persist across shell commands.
    env_file = os.environ.get("TABVIS_ENV_FILE")
    if env_file:
        try:
            env_script = (
                await asyncio.to_thread(
                    lambda: Path(env_file).read_text(encoding="utf-8")
                )
            ).strip()
            if env_script:
                scripts.append(env_script)
                log_for_debugging(
                    f"Session environment loaded from TABVIS_ENV_FILE: {env_file} "
                    f"({len(env_script)} chars)"
                )
        except Exception as e:  # noqa: BLE001
            code = get_errno_code(e)
            if code != "ENOENT":
                log_for_debugging(f"Failed to read TABVIS_ENV_FILE: {get_error_message(e)}")

    # Load hook environment files from the session directory.
    session_env_dir = await get_session_env_dir_path()
    try:
        files = await asyncio.to_thread(os.listdir, session_env_dir)
        # Sort the hook env files by their declared priority/order so the resulting env is
        # deterministic.
        hook_files = sorted(
            (f for f in files if HOOK_ENV_REGEX.match(f)),
            key=_hook_env_sort_key,
        )

        for file in hook_files:
            file_path = os.path.join(session_env_dir, file)
            try:
                content = (
                    await asyncio.to_thread(
                        lambda p=file_path: Path(p).read_text(encoding="utf-8")
                    )
                ).strip()
                if content:
                    scripts.append(content)
            except Exception as e:  # noqa: BLE001
                code = get_errno_code(e)
                if code != "ENOENT":
                    log_for_debugging(
                        f"Failed to read hook file {file_path}: {get_error_message(e)}"
                    )

        if len(hook_files) > 0:
            log_for_debugging(
                f"Session environment loaded from {len(hook_files)} hook file(s)"
            )
    except Exception as e:  # noqa: BLE001
        code = get_errno_code(e)
        if code != "ENOENT":
            log_for_debugging(
                f"Failed to load session environment from hooks: {get_error_message(e)}"
            )

    if len(scripts) == 0:
        log_for_debugging("No session environment scripts found")
        _session_env_script = None
        return _session_env_script

    _session_env_script = "\n".join(scripts)
    log_for_debugging(
        f"Session environment script ready ({len(_session_env_script)} chars total)"
    )
    return _session_env_script


HOOK_ENV_PRIORITY: dict[str, int] = {
    "setup": 0,
    "sessionstart": 1,
    "cwdchanged": 2,
    "filechanged": 3,
}
HOOK_ENV_REGEX = re.compile(r"^(setup|sessionstart|cwdchanged|filechanged)-hook-(\d+)\.sh$")


def _hook_env_sort_key(name: str) -> tuple[int, int]:
    """Sort key mirroring ``sortHookEnvFiles``: (type priority, numeric index)."""
    match = HOOK_ENV_REGEX.match(name)
    type_ = match.group(1) if match else ""
    priority = HOOK_ENV_PRIORITY.get(type_, 99)
    index = int(match.group(2)) if match else 0
    return (priority, index)
