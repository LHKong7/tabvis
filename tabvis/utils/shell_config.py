"""Shell configuration file helpers

Utilities for managing shell configuration files (``.bashrc`` / ``.zshrc`` / fish ``config.fish``)
used for managing tabvis aliases and PATH entries.

Casing: Python identifiers are snake_case. :func:`get_shell_config_paths` returns a plain
``dict[str, str]`` keyed by shell family name (``zsh`` / ``bash`` / ``fish``) — those keys are
internal, not a wire contract, but kept verbatim for parity with the TS map shape.

Faithful-behavior notes:
- ``readFile(filePath, {encoding:'utf8'})`` →
  :meth:`tabvis.utils.fs_operations.FsOperations.read_file` (async, utf8). A filesystem-inaccessible
  error (``isFsInaccessible``) → return ``None`` (file missing / unreadable); any other error
  re-raises, exactly like the TS catch.
- ``open(filePath, 'w')`` + ``writeFile`` + ``datasync`` → an ``os.open(O_WRONLY|O_CREAT|O_TRUNC)``
  write followed by :func:`os.fsync` (the ``datasync`` durability guarantee) then close.
- ``getLocalTabvisPath`` is the REAL implemented :func:`tabvis.utils.local_installer.get_local_tabvis_path`.
"""

from __future__ import annotations

import asyncio
import os
import os.path as _osp
import re

from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.errors import get_errno_code
from tabvis.utils.fs_operations import get_fs_implementation


def get_local_tabvis_path() -> str:
    """Path to the managed ``tabvis`` wrapper script (``<config-home>/local/tabvis``)."""
    return _osp.join(get_tabvis_config_home_dir(), "local", "tabvis")

# ``tabvis.utils.errors`` (only the errno predicates landed). Faithful local fallback below;
# replace with ``from tabvis.utils.errors import is_fs_inaccessible`` once that lands.
_FS_INACCESSIBLE_CODES = frozenset({"ENOENT", "EACCES", "EPERM", "ENOTDIR", "ELOOP"})


def is_fs_inaccessible(error: object) -> bool:
    """Whether ``error`` is an expected "nothing there / no access" filesystem error.

    Covers ENOENT/EACCES/EPERM/ENOTDIR/ELOOP.
    """
    return get_errno_code(error) in _FS_INACCESSIBLE_CODES

# Matches an ``alias tabvis=`` line (leading whitespace tolerated). Kept as a module constant for
# parity with the TS ``TABVIS_ALIAS_REGEX`` export.
TABVIS_ALIAS_REGEX = re.compile(r"^\s*alias\s+tabvis\s*=")

# Sub-patterns used to extract the alias target (mirrors the inline TS regexes).
_ALIAS_TARGET_QUOTED_RE = re.compile(r"""alias\s+tabvis\s*=\s*["']([^"']+)["']""")
_ALIAS_TARGET_BARE_RE = re.compile(r"alias\s+tabvis\s*=\s*([^#\n]+)")
_ALIAS_TARGET_FIND_RE = re.compile(r"""alias\s+tabvis=["']?([^"'\s]+)""")


def get_shell_config_paths(
    *,
    env: dict[str, str] | None = None,
    homedir: str | None = None,
) -> dict[str, str]:
    """Get the paths to shell configuration files.

    Respects ``ZDOTDIR`` for zsh users. ``env`` / ``homedir`` are optional overrides (for
    testing); they default to ``os.environ`` and the user's home directory respectively.
    """
    home = homedir if homedir is not None else _osp.expanduser("~")
    environment = env if env is not None else dict(os.environ)
    zsh_config_dir = environment.get("ZDOTDIR") or home
    return {
        "zsh": _osp.join(zsh_config_dir, ".zshrc"),
        "bash": _osp.join(home, ".bashrc"),
        "fish": _osp.join(home, ".config/fish/config.fish"),
    }


def filter_tabvis_aliases(lines: list[str]) -> dict[str, object]:
    """Filter out installer-created tabvis aliases from an array of lines.

    Only removes aliases pointing to ``$HOME/.tabvis/local/tabvis`` (the installer location).
    Preserves custom user aliases that point to other locations.

    Returns ``{"filtered": list[str], "had_alias": bool}`` — ``had_alias`` is ``True`` when the
    default installer alias was found and removed.
    """
    had_alias = False
    filtered: list[str] = []
    for line in lines:
        if TABVIS_ALIAS_REGEX.search(line):
            # Extract the alias target — handle spaces, quotes, and various formats.
            # First try with quotes, then fall back to the unquoted form (up to a comment / EOL).
            match = _ALIAS_TARGET_QUOTED_RE.search(line)
            if not match:
                match = _ALIAS_TARGET_BARE_RE.search(line)

            if match and match.group(1):
                target = match.group(1).strip()
                # Only remove if it points to the installer location. The installer always
                # creates aliases with the full expanded path.
                if target == get_local_tabvis_path():
                    had_alias = True
                    continue  # Remove this line.
            # Keep custom aliases that don't point to the installer location.
        filtered.append(line)
    return {"filtered": filtered, "had_alias": had_alias}


async def read_file_lines(file_path: str) -> list[str] | None:
    """Read a file and split it into lines. Returns ``None`` if it doesn't exist / can't be read."""
    try:
        content = await get_fs_implementation().read_file(file_path, {"encoding": "utf8"})
        return content.split("\n")
    except Exception as e:  # noqa: BLE001 - faithful TS catch (classify then re-raise)
        if is_fs_inaccessible(e):
            return None
        raise


async def write_file_lines(file_path: str, lines: list[str]) -> None:
    """Write lines back to a file (durable: write then fsync before returning)."""

    def _write() -> None:
        payload = "\n".join(lines).encode("utf-8")
        fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            os.write(fd, payload)
            # TS used fh.datasync() (fdatasync) for durability before close.
            try:
                os.fdatasync(fd)
            except (AttributeError, OSError):
                os.fsync(fd)
        finally:
            os.close(fd)

    await asyncio.to_thread(_write)


async def find_tabvis_alias(
    *,
    env: dict[str, str] | None = None,
    homedir: str | None = None,
) -> str | None:
    """Check if a tabvis alias exists in any shell config file.

    Returns the alias target if found, ``None`` otherwise.
    """
    configs = get_shell_config_paths(env=env, homedir=homedir)

    for config_path in configs.values():
        lines = await read_file_lines(config_path)
        if not lines:
            continue

        for line in lines:
            if TABVIS_ALIAS_REGEX.search(line):
                # Extract the alias target.
                match = _ALIAS_TARGET_FIND_RE.search(line)
                if match and match.group(1):
                    return match.group(1)

    return None


async def find_valid_tabvis_alias(
    *,
    env: dict[str, str] | None = None,
    homedir: str | None = None,
) -> str | None:
    """Check if a tabvis alias exists and points to a valid executable.

    Returns the alias target if valid, ``None`` otherwise.
    """
    alias_target = await find_tabvis_alias(env=env, homedir=homedir)
    if not alias_target:
        return None

    home = homedir if homedir is not None else _osp.expanduser("~")

    # Expand ~ to home directory (first occurrence only, mirroring TS String.replace).
    if alias_target.startswith("~"):
        expanded_path = alias_target.replace("~", home, 1)
    else:
        expanded_path = alias_target

    # Check if the target exists and is a file or symlink (executable or otherwise).
    try:
        stats = await get_fs_implementation().stat(expanded_path)
        if stats.is_file() or stats.is_symbolic_link():
            return alias_target
    except OSError:
        # Target doesn't exist or can't be accessed.
        pass

    return None
