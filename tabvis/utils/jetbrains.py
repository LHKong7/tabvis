"""JetBrains plugin detection

Locates the tabvis JetBrains-plugin install directory across the platform-specific JetBrains
config layouts (macOS ``~/Library/Application Support/JetBrains``, Windows ``%APPDATA%`` /
``%LOCALAPPDATA%``, Linux ``~/.config``/``~/.local/share``) and answers whether the plugin is
installed. The async detection is wrapped in a per-``IdeType`` memo (mirroring the TS
promise/result cache pair) plus a sync read of the last resolved value for status-notice checks.

Casing: Python identifiers are snake_case. ``IdeType`` is a plain ``str`` alias (the literal
union lives in :mod:`tabvis.utils.ide`); we accept any string here. The data round-tripped is just
filesystem paths (plain ``str``), so there are no wire-key dicts to preserve.

Reuses :func:`tabvis.utils.fs_operations.get_fs_implementation` for the swappable fs surface, so
tests can monkeypatch the fs implementation rather than the real disk.
"""

from __future__ import annotations

import os
import re
from os.path import join

from tabvis.utils.fs_operations import get_fs_implementation

# IdeType is the literal union from ide.ts; at runtime it is just a str.
IdeType = str

PLUGIN_PREFIX = "tabvis-jetbrains-plugin"

# Map of IDE names to their directory patterns.
IDE_NAME_TO_DIR_MAP: dict[str, list[str]] = {
    "pycharm": ["PyCharm"],
    "intellij": ["IntelliJIdea", "IdeaIC"],
    "webstorm": ["WebStorm"],
    "phpstorm": ["PhpStorm"],
    "rubymine": ["RubyMine"],
    "clion": ["CLion"],
    "goland": ["GoLand"],
    "rider": ["Rider"],
    "datagrip": ["DataGrip"],
    "appcode": ["AppCode"],
    "dataspell": ["DataSpell"],
    "aqua": ["Aqua"],
    "gateway": ["Gateway"],
    "fleet": ["Fleet"],
    "androidstudio": ["AndroidStudio"],
}


def _platform() -> str:
    """Return the Node ``os.platform()`` string for the running OS.

    Maps Python's :data:`sys.platform`/``os.name`` onto Node's ``'darwin'`` / ``'win32'`` /
    ``'linux'`` values used by the TS ``switch (platform())``.
    """
    import sys

    if sys.platform == "darwin":
        return "darwin"
    if os.name == "nt" or sys.platform.startswith("win"):
        return "win32"
    return "linux"


def build_common_plugin_directory_paths(ide_name: str) -> list[str]:
    """Build the candidate JetBrains plugin directory roots for ``ide_name``.

    https://www.jetbrains.com/help/pycharm/directories-used-by-the-ide-to-store-settings-caches-plugins-and-logs.html#plugins-directory
    """
    home_dir = os.path.expanduser("~")
    directories: list[str] = []
    ide_patterns = IDE_NAME_TO_DIR_MAP.get(ide_name.lower())
    if not ide_patterns:
        return directories

    app_data = os.environ.get("APPDATA") or join(home_dir, "AppData", "Roaming")
    local_app_data = os.environ.get("LOCALAPPDATA") or join(home_dir, "AppData", "Local")

    plat = _platform()
    if plat == "darwin":
        directories.append(join(home_dir, "Library", "Application Support", "JetBrains"))
        directories.append(join(home_dir, "Library", "Application Support"))
    elif plat == "win32":
        directories.append(join(app_data, "JetBrains"))
        directories.append(join(local_app_data, "JetBrains"))
        directories.append(app_data)
    elif plat == "linux":
        directories.append(join(home_dir, ".config", "JetBrains"))
        directories.append(join(home_dir, ".local", "share", "JetBrains"))
        for pattern in ide_patterns:
            directories.append(join(home_dir, "." + pattern))
    # default: leave directories empty

    return directories


async def detect_plugin_directories(ide_name: str) -> list[str]:
    """Find all actual plugin directories that exist for ``ide_name`` (deduplicated)."""
    found_directories: list[str] = []
    fs = get_fs_implementation()

    plugin_dir_paths = build_common_plugin_directory_paths(ide_name)
    ide_patterns = IDE_NAME_TO_DIR_MAP.get(ide_name.lower())
    if not ide_patterns:
        return found_directories

    # Precompile once — ide_patterns is invariant across base dirs.
    regexes = [re.compile("^" + p) for p in ide_patterns]

    for base_dir in plugin_dir_paths:
        try:
            entries = await fs.readdir(base_dir)
        except Exception:  # noqa: BLE001 - stale IDE dirs (ENOENT, EACCES, etc.)
            continue
        for regex in regexes:
            for entry in entries:
                if not regex.match(entry.name):
                    continue
                # Accept symlinks too — dirent.is_directory() is false for symlinks, but GNU
                # stow users symlink their JetBrains config dirs. Downstream fs.stat() calls
                # filter out symlinks that don't point to dirs.
                if not entry.is_directory() and not entry.is_symbolic_link():
                    continue
                directory = join(base_dir, entry.name)
                # Linux is the only OS to not have a plugins directory.
                if _platform() == "linux":
                    found_directories.append(directory)
                    continue
                plugin_dir = join(directory, "plugins")
                try:
                    await fs.stat(plugin_dir)
                    found_directories.append(plugin_dir)
                except Exception:  # noqa: BLE001 - plugin dir doesn't exist, skip
                    pass

    # Deduplicate preserving first-seen order (TS: filter indexOf === index).
    seen: set[str] = set()
    deduped: list[str] = []
    for directory in found_directories:
        if directory not in seen:
            seen.add(directory)
            deduped.append(directory)
    return deduped


async def is_jet_brains_plugin_installed(ide_type: IdeType) -> bool:
    """Whether the tabvis JetBrains plugin is installed for ``ide_type``."""
    plugin_dirs = await detect_plugin_directories(ide_type)
    for directory in plugin_dirs:
        plugin_path = join(directory, PLUGIN_PREFIX)
        try:
            await get_fs_implementation().stat(plugin_path)
            return True
        except Exception:  # noqa: BLE001 - plugin not in this dir, continue
            pass
    return False


_plugin_installed_cache: dict[IdeType, bool] = {}
# Async result cache: stores the in-flight task so concurrent callers dedup (TS Promise cache).
_plugin_installed_promise_cache: dict[IdeType, object] = {}


async def _is_jet_brains_plugin_installed_memoized(
    ide_type: IdeType,
    force_refresh: bool = False,
) -> bool:
    if not force_refresh:
        existing = _plugin_installed_promise_cache.get(ide_type)
        if existing is not None:
            return await existing  # type: ignore[no-any-return]

    import asyncio

    async def _run() -> bool:
        result = await is_jet_brains_plugin_installed(ide_type)
        _plugin_installed_cache[ide_type] = result
        return result

    task = asyncio.ensure_future(_run())
    _plugin_installed_promise_cache[ide_type] = task
    return await task


async def is_jet_brains_plugin_installed_cached(
    ide_type: IdeType,
    force_refresh: bool = False,
) -> bool:
    """Cached :func:`is_jet_brains_plugin_installed` (TS ``isJetBrainsPluginInstalledCached``)."""
    if force_refresh:
        _plugin_installed_cache.pop(ide_type, None)
        _plugin_installed_promise_cache.pop(ide_type, None)
    return await _is_jet_brains_plugin_installed_memoized(ide_type, force_refresh)


def is_jet_brains_plugin_installed_cached_sync(ide_type: IdeType) -> bool:
    """Synchronous read of the cached result; ``False`` if not yet resolved.

    Use this only in sync contexts (e.g. status-notice ``is_active`` checks).
    """
    return _plugin_installed_cache.get(ide_type, False)
