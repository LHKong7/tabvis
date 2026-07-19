"""Dynamic environment detectors

These are the env detectors that require :func:`tabvis.utils.exec_file_no_throw.exec_file_no_throw`
(a subprocess) and therefore cannot live in the dependency-light ``env.ts`` /
:mod:`tabvis.utils.env`. The module exposes:

* :func:`get_is_docker` — (memoized) whether we're inside a Docker container (Linux + ``/.dockerenv``).
* :func:`get_is_bubblewrap_sandbox` — Linux + ``TABVIS_BUBBLEWRAP`` truthy.
* :func:`is_musl_environment` — MUSL-vs-glibc detection (runtime ``stat`` fallback; cache populated
  at import on Linux).
* :func:`get_terminal_with_jetbrains_detection` / ``…_async`` — terminal name, with finer-grained
  JetBrains-IDE detection on Linux/Windows via the parent process command line.
* :func:`init_jetbrains_detection` — populate the JetBrains cache early in app init.
* :data:`env_dynamic` — the combined object: all of :data:`tabvis.utils.env.env`'s fields plus these
  dynamic functions and the (sync-detected) :attr:`terminal`.

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``execFileNoThrow`` → :func:`tabvis.utils.exec_file_no_throw.exec_file_no_throw`.
  ``getAncestorCommandsAsync`` → :func:`tabvis.utils.generic_process_utils.get_ancestor_commands_async`.
  ``isEnvTruthy`` → :func:`tabvis.utils.env_utils.is_env_truthy`. ``env``/``JETBRAINS_IDES`` →
  :mod:`tabvis.utils.env`.
- lodash ``memoize`` over the zero-arg async ``getIsDocker`` → a single-slot async memo (caches the
  first result), since :func:`functools.lru_cache` can't wrap coroutines.
- ``process.platform`` → :data:`sys.platform` (``linux``/``darwin``/``win32``, Node-identical
  values). ``stat('/lib/libc.musl-…')`` → :func:`os.stat` off-thread, mirroring the TS
  fire-and-forget cache population at module load.
- The two ``if (false) return …`` lines in ``isMuslEnvironment`` are dead compile-time
  feature-flag branches (``IS_LIBC_MUSL``/``IS_LIBC_GLIBC`` both false in this build) → dropped,
  leaving only the runtime fallback (faithful to the resolved external build).
- ``env_dynamic`` is a plain runtime object (no wire-key dict round-trips); identifiers snake_case.
"""

from __future__ import annotations

import os
import sys

from tabvis.utils.env import JETBRAINS_IDES, env
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.exec_file_no_throw import exec_file_no_throw
from tabvis.utils.generic_process_utils import get_ancestor_commands_async

# --- getIsDocker (memoized async) ---

_IS_DOCKER_CACHED = False
_IS_DOCKER_VALUE = False


async def get_is_docker() -> bool:
    """Whether we're running inside a Docker container (Linux + ``/.dockerenv``). Memoized."""
    global _IS_DOCKER_CACHED, _IS_DOCKER_VALUE
    if _IS_DOCKER_CACHED:
        return _IS_DOCKER_VALUE
    if sys.platform != "linux":
        _IS_DOCKER_VALUE = False
        _IS_DOCKER_CACHED = True
        return False
    result = await exec_file_no_throw("test", ["-f", "/.dockerenv"])
    _IS_DOCKER_VALUE = result["code"] == 0
    _IS_DOCKER_CACHED = True
    return _IS_DOCKER_VALUE


def get_is_bubblewrap_sandbox() -> bool:
    """Whether we're running inside a bubblewrap sandbox (Linux + ``TABVIS_BUBBLEWRAP`` truthy)."""
    return sys.platform == "linux" and is_env_truthy(os.environ.get("TABVIS_BUBBLEWRAP"))


# --- MUSL detection ---

# Cache for the runtime musl detection fallback (node/unbundled only). In native builds, feature
# flags resolve this at compile time; here both flags are false, so the cache is always consulted.
_musl_runtime_cache: bool | None = None


def _populate_musl_cache_blocking() -> None:
    """Populate :data:`_musl_runtime_cache` by stat-ing the musl libc shared object (Linux only)."""
    global _musl_runtime_cache
    if sys.platform != "linux":
        return
    import platform as _platform

    musl_arch = "x86_64" if _platform.machine().lower() in ("x86_64", "amd64") else "aarch64"
    try:
        os.stat(f"/lib/libc.musl-{musl_arch}.so.1")
        _musl_runtime_cache = True
    except OSError:
        _musl_runtime_cache = False


# Fire-and-forget at import (TS: ``void stat(...).then(...)``). On Linux this performs a single,
# cheap stat; on other platforms it is a no-op.
if sys.platform == "linux":
    _populate_musl_cache_blocking()


def is_musl_environment() -> bool:
    """Whether the system uses MUSL libc instead of glibc.

    In native linux builds this is statically known via feature flags; in this (external) build
    both flags are false, so it falls back to the runtime ``stat`` cache populated at import. If
    the cache isn't populated, returns ``False``.
    """
    if sys.platform != "linux":
        return False
    return _musl_runtime_cache if _musl_runtime_cache is not None else False


# --- JetBrains IDE detection ---

# Cache for async JetBrains detection. Sentinel ``_UNSET`` distinguishes "not yet detected" from
# a detected ``None`` (TS uses ``undefined`` vs ``null``).
_UNSET = object()
_jetbrains_ide_cache: object | str | None = _UNSET


async def _detect_jetbrains_ide_from_parent_process_async() -> str | None:
    global _jetbrains_ide_cache
    if _jetbrains_ide_cache is not _UNSET:
        return _jetbrains_ide_cache  # type: ignore[return-value]

    if sys.platform == "darwin":
        # macOS uses bundle-ID detection, already handled in env.terminal.
        _jetbrains_ide_cache = None
        return None

    try:
        # Get ancestor commands in a single call (avoids sync bash in a loop).
        commands = await get_ancestor_commands_async(os.getpid(), 10)
        for command in commands:
            lower_command = command.lower()
            for ide in JETBRAINS_IDES:
                if ide in lower_command:
                    _jetbrains_ide_cache = ide
                    return ide
    except Exception:  # noqa: BLE001 - best-effort detection; silently fail
        pass

    _jetbrains_ide_cache = None
    return None


async def get_terminal_with_jetbrains_detection_async() -> str | None:
    """Terminal name, with finer-grained JetBrains-IDE detection on Linux/Windows (async)."""
    if os.environ.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm":
        # macOS bundle-ID detection above already handles JetBrains IDEs.
        if env.platform != "darwin":
            specific_ide = await _detect_jetbrains_ide_from_parent_process_async()
            return specific_ide or "pycharm"
    return env.terminal


def get_terminal_with_jetbrains_detection() -> str | None:
    """Synchronous terminal name, using the cached JetBrains result or falling back to
    ``env.terminal``.

    Callers should prefer :func:`get_terminal_with_jetbrains_detection_async`; the async version
    should be called early in app init to populate the cache.
    """
    if os.environ.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm":
        if env.platform != "darwin":
            if _jetbrains_ide_cache is not _UNSET:
                return _jetbrains_ide_cache or "pycharm"  # type: ignore[return-value]
            return "pycharm"
    return env.terminal


async def init_jetbrains_detection() -> None:
    """Initialize JetBrains IDE detection asynchronously (call early in app init).

    After this resolves, :func:`get_terminal_with_jetbrains_detection` returns accurate results.
    """
    if os.environ.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm":
        await _detect_jetbrains_ide_from_parent_process_async()


def _reset_caches_for_tests() -> None:
    """Reset the memoized docker/musl/jetbrains caches. Tests only."""
    global _IS_DOCKER_CACHED, _IS_DOCKER_VALUE, _musl_runtime_cache, _jetbrains_ide_cache
    _IS_DOCKER_CACHED = False
    _IS_DOCKER_VALUE = False
    _musl_runtime_cache = None
    _jetbrains_ide_cache = _UNSET


class _EnvDynamic:
    """Combined export: all of :data:`tabvis.utils.env.env`'s fields plus the dynamic detectors.

        spread — copies env's attributes onto this object, then overrides ``terminal`` with the
    sync JetBrains-aware value and attaches the dynamic functions.
    """

    def __init__(self) -> None:
        # Spread ``...env`` — copy every public attribute of the env singleton.
        for name in vars(env):
            setattr(self, name, getattr(env, name))
        # Override / attach dynamic members (TS: ``terminal: getTerminalWithJetBrainsDetection()``).
        self.terminal = get_terminal_with_jetbrains_detection()
        self.get_is_docker = get_is_docker
        self.get_is_bubblewrap_sandbox = get_is_bubblewrap_sandbox
        self.is_musl_environment = is_musl_environment
        self.get_terminal_with_jetbrains_detection_async = (
            get_terminal_with_jetbrains_detection_async
        )
        self.init_jetbrains_detection = init_jetbrains_detection


env_dynamic = _EnvDynamic()
