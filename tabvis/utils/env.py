"""Runtime environment detection

Builds the module-level :data:`env` singleton (platform / arch / node-version / terminal / CI /
SSH / package-manager + runtime detectors / deployment-environment) plus the standalone
:func:`get_global_tabvis_file` and :func:`get_host_platform_for_analytics` helpers.

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- lodash ``memoize`` → :func:`functools.lru_cache` for the zero-arg getters; ``memoize`` over a
  zero-arg fn caches the first result (call once, reuse) — :func:`functools.lru_cache` matches.
- ``process.platform`` → :data:`sys.platform` (``win32`` / ``darwin`` / ``linux``, Node-identical
  values). ``process.arch`` → a Node-style arch string derived from :data:`platform.machine`.
  ``process.version`` → the Python version (there is no Node; we report the CPython version as the
  runtime version, mirroring "the interpreter we run on").
- ``which`` is the async :func:`tabvis.utils.which.which`; ``isCommandAvailable`` awaits it. The
  package-manager / runtime detectors are therefore async + memoized (single-slot async memo,
  since :func:`functools.lru_cache` can't wrap coroutines).
- ``hasInternetAccess`` used ``axios.head('http://1.1.1.1', { signal: timeout(1000) })``. There is
  no ``axios`` here; we replicate the 1s-timeout HEAD-style reachability probe with the stdlib
  :mod:`urllib.request` (recorded in ``deps_needed`` as the dropped ``axios`` dependency).
- ``getFsImplementation().existsSync`` → :func:`tabvis.utils.fs_operations.get_fs_implementation`.
- ``findExecutable('npm', [])`` → :func:`tabvis.utils.find_executable.find_executable`.
- ``isEnvTruthy`` → :func:`tabvis.utils.env_utils.is_env_truthy`. The TS ``getTabvisConfigHomeDir``
  import is unused in ``env.ts`` and is dropped here (ruff-clean).
- The :data:`env` object's string fields (``platform``/``arch``/``nodeVersion``/``terminal``) and
  dict-ish detector results are plain runtime values (not wire dicts); identifiers are snake_case.
"""

from __future__ import annotations

import functools
import os
import platform as _platform
import sys
import urllib.error
import urllib.request
from os.path import join
from pathlib import Path
from typing import Literal

from tabvis.utils.bundled_mode import is_running_with_bun
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.find_executable import find_executable
from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.which import which

Platform = Literal["win32", "darwin", "linux"]


def _homedir() -> str:
    return str(Path.home())


# Config and data paths
@functools.lru_cache(maxsize=1)
def get_global_tabvis_file() -> str:
    """Path to ``.tabvis.json`` under ``TABVIS_CONFIG_DIR`` (or the home directory)."""
    filename = ".tabvis.json"
    return join(os.environ.get("TABVIS_CONFIG_DIR") or _homedir(), filename)


# Single-slot async memo for hasInternetAccess (lodash memoize over a zero-arg async fn).
_HAS_INTERNET_CACHED = False
_HAS_INTERNET_VALUE = False


async def _has_internet_access() -> bool:
    global _HAS_INTERNET_CACHED, _HAS_INTERNET_VALUE
    if _HAS_INTERNET_CACHED:
        return _HAS_INTERNET_VALUE

    def _probe() -> bool:
        try:
            req = urllib.request.Request("http://1.1.1.1", method="HEAD")
            with urllib.request.urlopen(req, timeout=1):  # noqa: S310 - fixed literal host
                return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    import asyncio

    result = await asyncio.to_thread(_probe)
    _HAS_INTERNET_VALUE = result
    _HAS_INTERNET_CACHED = True
    return result


async def _is_command_available(command: str) -> bool:
    try:
        # which does not execute the file.
        return bool(await which(command))
    except Exception:  # noqa: BLE001 - TS `catch { return false }`
        return False


# Single-slot async memos for the package-manager / runtime detectors.
_PACKAGE_MANAGERS_CACHED = False
_PACKAGE_MANAGERS_VALUE: list[str] = []
_RUNTIMES_CACHED = False
_RUNTIMES_VALUE: list[str] = []


async def _detect_package_managers() -> list[str]:
    global _PACKAGE_MANAGERS_CACHED, _PACKAGE_MANAGERS_VALUE
    if _PACKAGE_MANAGERS_CACHED:
        return _PACKAGE_MANAGERS_VALUE

    package_managers: list[str] = []
    if await _is_command_available("npm"):
        package_managers.append("npm")
    if await _is_command_available("yarn"):
        package_managers.append("yarn")
    if await _is_command_available("pnpm"):
        package_managers.append("pnpm")

    _PACKAGE_MANAGERS_VALUE = package_managers
    _PACKAGE_MANAGERS_CACHED = True
    return package_managers


async def _detect_runtimes() -> list[str]:
    global _RUNTIMES_CACHED, _RUNTIMES_VALUE
    if _RUNTIMES_CACHED:
        return _RUNTIMES_VALUE

    runtimes: list[str] = []
    if await _is_command_available("bun"):
        runtimes.append("bun")
    if await _is_command_available("deno"):
        runtimes.append("deno")
    if await _is_command_available("node"):
        runtimes.append("node")

    _RUNTIMES_VALUE = runtimes
    _RUNTIMES_CACHED = True
    return runtimes


@functools.lru_cache(maxsize=1)
def _is_wsl_environment() -> bool:
    """Whether we're running in a WSL environment."""
    try:
        # Check for WSLInterop file which is a reliable indicator of WSL.
        return get_fs_implementation().exists_sync("/proc/sys/fs/binfmt_misc/WSLInterop")
    except Exception:  # noqa: BLE001 - if there's an error checking, assume not WSL
        return False


@functools.lru_cache(maxsize=1)
def _is_npm_from_windows_path() -> bool:
    """Whether the npm executable is located in the Windows filesystem within WSL
    (``/mnt/c/...``)."""
    try:
        # Only relevant in WSL environment.
        if not _is_wsl_environment():
            return False
        # Find the actual npm executable path.
        cmd = str(find_executable("npm", [])["cmd"])
        # If npm is in Windows path, it will start with /mnt/c/.
        return cmd.startswith("/mnt/c/")
    except Exception:  # noqa: BLE001 - if there's an error, assume it's not from Windows
        return False


def _is_conductor() -> bool:
    """Whether we're running via Conductor."""
    return os.environ.get("__CFBundleIdentifier") == "com.conductor.app"


JETBRAINS_IDES = [
    "pycharm",
    "intellij",
    "webstorm",
    "phpstorm",
    "rubymine",
    "clion",
    "goland",
    "rider",
    "datagrip",
    "appcode",
    "dataspell",
    "aqua",
    "gateway",
    "fleet",
    "jetbrains",
    "androidstudio",
]


def _detect_terminal() -> str | None:
    """Detect terminal type with fallbacks for all platforms."""
    e = os.environ
    if e.get("CURSOR_TRACE_ID"):
        return "cursor"
    # Cursor and Windsurf under WSL have TERM_PROGRAM=vscode.
    askpass = e.get("VSCODE_GIT_ASKPASS_MAIN")
    if askpass and "cursor" in askpass:
        return "cursor"
    if askpass and "windsurf" in askpass:
        return "windsurf"
    if askpass and "antigravity" in askpass:
        return "antigravity"
    bundle_id_raw = e.get("__CFBundleIdentifier")
    bundle_id = bundle_id_raw.lower() if bundle_id_raw else None
    if bundle_id and "vscodium" in bundle_id:
        return "codium"
    if bundle_id and "windsurf" in bundle_id:
        return "windsurf"
    # Check for JetBrains IDEs in bundle ID.
    if bundle_id:
        for ide in JETBRAINS_IDES:
            if ide in bundle_id:
                return ide

    if e.get("VisualStudioVersion"):
        # This is desktop Visual Studio, not VS Code.
        return "visualstudio"

    # Check for JetBrains terminal on Linux/Windows.
    if e.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm":
        # For macOS, bundle ID detection above already handles JetBrains IDEs.
        if sys.platform == "darwin":
            return "pycharm"
        # For finegrained detection on Linux/Windows use envDynamic.
        return "pycharm"

    # Check for specific terminals by TERM before TERM_PROGRAM.
    term = e.get("TERM")
    if term == "xterm-ghostty":
        return "ghostty"
    if term and "kitty" in term:
        return "kitty"

    if e.get("TERM_PROGRAM"):
        return e.get("TERM_PROGRAM")

    if e.get("TMUX"):
        return "tmux"
    if e.get("STY"):
        return "screen"

    # Check for terminal-specific environment variables (common on Linux).
    if e.get("KONSOLE_VERSION"):
        return "konsole"
    if e.get("GNOME_TERMINAL_SERVICE"):
        return "gnome-terminal"
    if e.get("XTERM_VERSION"):
        return "xterm"
    if e.get("VTE_VERSION"):
        return "vte-based"
    if e.get("TERMINATOR_UUID"):
        return "terminator"
    if e.get("KITTY_WINDOW_ID"):
        return "kitty"
    if e.get("ALACRITTY_LOG"):
        return "alacritty"
    if e.get("TILIX_ID"):
        return "tilix"

    # Windows-specific detection.
    if e.get("WT_SESSION"):
        return "windows-terminal"
    if e.get("SESSIONNAME") and e.get("TERM") == "cygwin":
        return "cygwin"
    if e.get("MSYSTEM"):
        return e["MSYSTEM"].lower()  # MINGW64, MSYS2, etc.
    if e.get("ConEmuANSI") or e.get("ConEmuPID") or e.get("ConEmuTask"):
        return "conemu"

    # WSL detection.
    if e.get("WSL_DISTRO_NAME"):
        return f"wsl-{e['WSL_DISTRO_NAME']}"

    # SSH session detection.
    if _is_ssh_session():
        return "ssh-session"

    # Fall back to TERM which is more universally available.
    if term:
        if "alacritty" in term:
            return "alacritty"
        if "rxvt" in term:
            return "rxvt"
        if "termite" in term:
            return "termite"
        return term

    # Detect non-interactive environment.
    if not sys.stdout.isatty():
        return "non-interactive"

    return None


@functools.lru_cache(maxsize=1)
def detect_deployment_environment() -> str:
    """Detect the deployment environment/platform from environment variables; ``'unknown'`` if
    not detected."""
    e = os.environ
    # Cloud development environments
    if is_env_truthy(e.get("CODESPACES")):
        return "codespaces"
    if e.get("GITPOD_WORKSPACE_ID"):
        return "gitpod"
    if e.get("REPL_ID") or e.get("REPL_SLUG"):
        return "replit"
    if e.get("PROJECT_DOMAIN"):
        return "glitch"

    # Cloud platforms
    if is_env_truthy(e.get("VERCEL")):
        return "vercel"
    if e.get("RAILWAY_ENVIRONMENT_NAME") or e.get("RAILWAY_SERVICE_NAME"):
        return "railway"
    if is_env_truthy(e.get("RENDER")):
        return "render"
    if is_env_truthy(e.get("NETLIFY")):
        return "netlify"
    if e.get("DYNO"):
        return "heroku"
    if e.get("FLY_APP_NAME") or e.get("FLY_MACHINE_ID"):
        return "fly.io"
    if is_env_truthy(e.get("CF_PAGES")):
        return "cloudflare-pages"
    if e.get("DENO_DEPLOYMENT_ID"):
        return "deno-deploy"
    if e.get("WEBSITE_SITE_NAME") or e.get("WEBSITE_SKU"):
        return "azure-app-service"
    if e.get("AZURE_FUNCTIONS_ENVIRONMENT"):
        return "azure-functions"
    app_url = e.get("APP_URL")
    if app_url and "ondigitalocean.app" in app_url:
        return "digitalocean-app-platform"
    if e.get("SPACE_CREATOR_USER_ID"):
        return "huggingface-spaces"

    # CI/CD platforms
    if is_env_truthy(e.get("GITHUB_ACTIONS")):
        return "github-actions"
    if is_env_truthy(e.get("GITLAB_CI")):
        return "gitlab-ci"
    if e.get("CIRCLECI"):
        return "circleci"
    if e.get("BUILDKITE"):
        return "buildkite"
    if is_env_truthy(e.get("CI")):
        return "ci"

    # Container orchestration
    if e.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    try:
        if get_fs_implementation().exists_sync("/.dockerenv"):
            return "docker"
    except Exception:  # noqa: BLE001 - ignore errors checking for Docker
        pass

    # Platform-specific fallback for undetected environments.
    if env.platform == "darwin":
        return "unknown-darwin"
    if env.platform == "linux":
        return "unknown-linux"
    if env.platform == "win32":
        return "unknown-win32"

    return "unknown"


# all of these should be immutable
def _is_ssh_session() -> bool:
    e = os.environ
    return bool(e.get("SSH_CONNECTION") or e.get("SSH_CLIENT") or e.get("SSH_TTY"))


def _node_arch() -> str:
    """Map :data:`platform.machine` to the Node ``process.arch`` vocabulary the callers expect."""
    machine = _platform.machine().lower()
    mapping = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "ia32",
        "i686": "ia32",
        "x86": "ia32",
    }
    return mapping.get(machine, machine)


def _detect_platform() -> Platform:
    return sys.platform if sys.platform in ("win32", "darwin") else "linux"  # type: ignore[return-value]


class _Env:
    """The module-level environment singleton.

    Field names mirror the TS object keys (snake_case here): :attr:`has_internet_access`,
    :attr:`is_ci`, :attr:`platform`, :attr:`arch`, :attr:`node_version`, :attr:`terminal`,
    :attr:`is_ssh`, :attr:`get_package_managers`, :attr:`get_runtimes`, :attr:`is_running_with_bun`,
    :attr:`is_wsl_environment`, :attr:`is_npm_from_windows_path`, :attr:`is_conductor`,
    :attr:`detect_deployment_environment`.
    """

    def __init__(self) -> None:
        self.has_internet_access = _has_internet_access
        self.is_ci = is_env_truthy(os.environ.get("CI"))
        self.platform: Platform = _detect_platform()
        self.arch = _node_arch()
        self.node_version = f"v{_platform.python_version()}"
        self.terminal = _detect_terminal()
        self.is_ssh = _is_ssh_session
        self.get_package_managers = _detect_package_managers
        self.get_runtimes = _detect_runtimes
        self.is_running_with_bun = functools.lru_cache(maxsize=1)(is_running_with_bun)
        self.is_wsl_environment = _is_wsl_environment
        self.is_npm_from_windows_path = _is_npm_from_windows_path
        self.is_conductor = _is_conductor
        self.detect_deployment_environment = detect_deployment_environment


env = _Env()


def get_host_platform_for_analytics() -> Platform:
    """Host platform for analytics reporting.

    If ``TABVIS_HOST_PLATFORM`` is a valid platform value it overrides the detected platform — useful
    for container/remote environments where the container OS differs from the host.
    """
    override = os.environ.get("TABVIS_HOST_PLATFORM")
    if override in ("win32", "darwin", "linux"):
        return override  # type: ignore[return-value]
    return env.platform
