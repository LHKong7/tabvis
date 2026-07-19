"""IDE / editor integration.

Detects running IDEs (VS Code family + JetBrains), reads/cleans up the IDE lockfiles under
``~/.tabvis/ide``, resolves the host IP / connection, installs the VS Code extension, and exposes
the diff-tab integration RPC. The JetBrains plugin detection lives in
:mod:`tabvis.utils.jetbrains`; the WSL/Windows path conversion in
:mod:`tabvis.utils.ide_path_conversion`.

Casing: Python identifiers snake_case, the literal IDE-type union as a module constant. The
lockfile JSON keeps its **wire keys verbatim** (``workspaceFolders`` / ``ideName`` / ``transport``
/ ``runningInWindows`` / ``authToken``) since it round-trips to the IDE extension on disk. The
:class:`DetectedIDEInfo` runtime payload mirrors the TS object shape with snake_case attributes.

Reuses: :mod:`tabvis.utils.fs_operations` (swappable fs), :mod:`tabvis.utils.exec_file_no_throw`
(subprocess), :mod:`tabvis.utils.ide_path_conversion`, :mod:`tabvis.utils.platform`,
:mod:`tabvis.utils.semver`, :mod:`tabvis.utils.env`/``env_dynamic``, :mod:`tabvis.utils.abort_controller`.
No network on the detection path; the ANT-only ``install_from_artifactory`` shells to a URL
fetched via stdlib ``urllib`` and is gated on ``USER_TYPE == 'ant'``.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from os.path import basename, join, sep
from typing import TYPE_CHECKING, Any, Literal

from tabvis.bootstrap.state import get_is_scroll_draining, get_original_cwd
from tabvis.bootstrap_macro import MACRO
from tabvis.utils.abort_controller import create_abort_controller
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env import env
from tabvis.utils.env_dynamic import env_dynamic
from tabvis.utils.env_utils import get_tabvis_config_home_dir, is_env_truthy
from tabvis.utils.errors import get_errno_code, get_error_message
from tabvis.utils.exec_file_no_throw import (
    exec_file_no_throw,
    exec_file_no_throw_with_cwd,
)
from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.generic_process_utils import get_ancestor_pids_async
from tabvis.utils.ide_path_conversion import (
    WindowsToWSLConverter,
    check_wsl_distro_match,
)
from tabvis.utils.jetbrains import is_jet_brains_plugin_installed_cached
from tabvis.utils.log import log_error
from tabvis.utils.memoize import memoize_with_ttl_async
from tabvis.utils.platform import get_platform
from tabvis.utils.semver import lt
from tabvis.utils.sleep import sleep
from tabvis.utils.slow_operations import json_parse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tabvis.agent.mcp.types import ConnectedMCPServer, MCPServerConnection

# Native path separator (Node ``path.sep``); aliased to UPPER_CASE per house style.
PATH_SEPARATOR = sep

# (notify connected / close diff tabs) lands with the IDE-MCP client wave; re-export the real
# function then. Until then re-expose a lazy resolver that raises a clear error if invoked.


async def call_ide_rpc(method: str, params: dict[str, Any], ide_client: Any) -> Any:
    """Lazy bridge to ``tabvis.agent.mcp.client.call_ide_rpc`` (not yet implemented)."""
    try:
        from tabvis.agent.mcp.client import (
            call_ide_rpc as _real_call_ide_rpc,  # type: ignore[attr-defined]
        )
    except Exception as exc:  # noqa: BLE001 - module not implemented yet
        raise NotImplementedError(
            "call_ide_rpc is not implemented yet (tabvis.agent.mcp.client.call_ide_rpc)"
        ) from exc
    return await _real_call_ide_rpc(method, params, ide_client)


def is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _make_ancestor_pid_lookup():
    """Returns a coroutine factory that lazily fetches our ancestor PID chain, caching within the
    closure's lifetime. Scope to a single detection pass — PIDs recycle over time."""
    cache: dict[str, Any] = {"task": None}

    def lookup() -> Any:
        if cache["task"] is None:

            async def _fetch() -> set[int]:
                pids = await get_ancestor_pids_async(os.getppid(), 10)
                return set(pids)

            cache["task"] = asyncio.ensure_future(_fetch())
        return cache["task"]

    return lookup


# IdeType literal union.
IdeType = Literal[
    "cursor",
    "windsurf",
    "vscode",
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
    "androidstudio",
]


@dataclass
class IdeLockfileInfo:
    workspace_folders: list[str]
    port: int
    use_web_socket: bool
    running_in_windows: bool
    pid: int | None = None
    ide_name: str | None = None
    auth_token: str | None = None


@dataclass
class DetectedIDEInfo:
    name: str
    port: int
    workspace_folders: list[str]
    url: str
    is_valid: bool
    auth_token: str | None = None
    ide_running_in_windows: bool | None = None


@dataclass(frozen=True)
class IdeConfig:
    ide_kind: Literal["vscode", "jetbrains"]
    display_name: str
    process_keywords_mac: list[str]
    process_keywords_windows: list[str]
    process_keywords_linux: list[str]


SUPPORTED_IDE_CONFIGS: dict[str, IdeConfig] = {
    "cursor": IdeConfig("vscode", "Cursor", ["Cursor Helper", "Cursor.app"], ["cursor.exe"], ["cursor"]),
    "windsurf": IdeConfig(
        "vscode", "Windsurf", ["Windsurf Helper", "Windsurf.app"], ["windsurf.exe"], ["windsurf"]
    ),
    "vscode": IdeConfig(
        "vscode", "VS Code", ["Visual Studio Code", "Code Helper"], ["code.exe"], ["code"]
    ),
    "intellij": IdeConfig(
        "jetbrains", "IntelliJ IDEA", ["IntelliJ IDEA"], ["idea64.exe"], ["idea", "intellij"]
    ),
    "pycharm": IdeConfig("jetbrains", "PyCharm", ["PyCharm"], ["pycharm64.exe"], ["pycharm"]),
    "webstorm": IdeConfig("jetbrains", "WebStorm", ["WebStorm"], ["webstorm64.exe"], ["webstorm"]),
    "phpstorm": IdeConfig("jetbrains", "PhpStorm", ["PhpStorm"], ["phpstorm64.exe"], ["phpstorm"]),
    "rubymine": IdeConfig("jetbrains", "RubyMine", ["RubyMine"], ["rubymine64.exe"], ["rubymine"]),
    "clion": IdeConfig("jetbrains", "CLion", ["CLion"], ["clion64.exe"], ["clion"]),
    "goland": IdeConfig("jetbrains", "GoLand", ["GoLand"], ["goland64.exe"], ["goland"]),
    "rider": IdeConfig("jetbrains", "Rider", ["Rider"], ["rider64.exe"], ["rider"]),
    "datagrip": IdeConfig("jetbrains", "DataGrip", ["DataGrip"], ["datagrip64.exe"], ["datagrip"]),
    "appcode": IdeConfig("jetbrains", "AppCode", ["AppCode"], ["appcode.exe"], ["appcode"]),
    "dataspell": IdeConfig(
        "jetbrains", "DataSpell", ["DataSpell"], ["dataspell64.exe"], ["dataspell"]
    ),
    # Do not auto-detect aqua/gateway/fleet on mac/linux — too common.
    "aqua": IdeConfig("jetbrains", "Aqua", [], ["aqua64.exe"], []),
    "gateway": IdeConfig("jetbrains", "Gateway", [], ["gateway64.exe"], []),
    "fleet": IdeConfig("jetbrains", "Fleet", [], ["fleet.exe"], []),
    "androidstudio": IdeConfig(
        "jetbrains", "Android Studio", ["Android Studio"], ["studio64.exe"], ["android-studio"]
    ),
}


def is_vscode_ide(ide: IdeType | None) -> bool:
    if not ide:
        return False
    config = SUPPORTED_IDE_CONFIGS.get(ide)
    return bool(config) and config.ide_kind == "vscode"  # type: ignore[union-attr]


def is_jet_brains_ide(ide: IdeType | None) -> bool:
    if not ide:
        return False
    config = SUPPORTED_IDE_CONFIGS.get(ide)
    return bool(config) and config.ide_kind == "jetbrains"  # type: ignore[union-attr]


# memoize(() => ...): a zero-arg cache. functools.cache gives the lodash-memoize semantics here.
_supported_vscode_terminal_cache: dict[int, bool] = {}
_supported_jetbrains_terminal_cache: dict[int, bool] = {}
_supported_terminal_cache: dict[int, bool] = {}


def is_supported_vscode_terminal() -> bool:
    if 0 not in _supported_vscode_terminal_cache:
        _supported_vscode_terminal_cache[0] = is_vscode_ide(env.terminal)  # type: ignore[arg-type]
    return _supported_vscode_terminal_cache[0]


def is_supported_jet_brains_terminal() -> bool:
    if 0 not in _supported_jetbrains_terminal_cache:
        _supported_jetbrains_terminal_cache[0] = is_jet_brains_ide(env_dynamic.terminal)  # type: ignore[arg-type]
    return _supported_jetbrains_terminal_cache[0]


def is_supported_terminal() -> bool:
    if 0 not in _supported_terminal_cache:
        _supported_terminal_cache[0] = (
            is_supported_vscode_terminal()
            or is_supported_jet_brains_terminal()
            or bool(os.environ.get("FORCE_CODE_TERMINAL"))
        )
    return _supported_terminal_cache[0]


def get_terminal_ide_type() -> IdeType | None:
    if not is_supported_terminal():
        return None
    return env.terminal  # type: ignore[return-value]


async def get_sorted_ide_lockfiles() -> list[str]:
    """Gets sorted IDE lockfiles from ``~/.tabvis/ide`` (full paths, newest mtime first)."""
    try:
        ide_lock_file_paths = await get_ide_lockfiles_paths()

        async def _collect(ide_lock_file_path: str) -> list[tuple[str, float]]:
            try:
                entries = await get_fs_implementation().readdir(ide_lock_file_path)
            except Exception as error:  # noqa: BLE001 - missing/inaccessible dirs expected
                if not _is_fs_inaccessible(error):
                    log_error(error)
                return []
            lock_entries = [f for f in entries if f.name.endswith(".lock")]

            async def _stat_entry(file: Any) -> tuple[str, float] | None:
                full_path = join(ide_lock_file_path, file.name)
                try:
                    file_stat = await get_fs_implementation().stat(full_path)
                    return (full_path, file_stat._st.st_mtime)
                except Exception:  # noqa: BLE001 - skip ones that fail
                    return None

            stats = await asyncio.gather(*[_stat_entry(f) for f in lock_entries])
            return [s for s in stats if s is not None]

        all_lockfiles = await asyncio.gather(
            *[_collect(p) for p in ide_lock_file_paths]
        )

        flattened: list[tuple[str, float]] = [item for sub in all_lockfiles for item in sub]
        flattened.sort(key=lambda item: item[1], reverse=True)
        return [path for path, _ in flattened]
    except Exception as error:  # noqa: BLE001
        log_error(error)
        return []


async def read_ide_lockfile(path: str) -> IdeLockfileInfo | None:
    try:
        content = await get_fs_implementation().read_file(path, {"encoding": "utf-8"})

        workspace_folders: list[str] = []
        pid: int | None = None
        ide_name: str | None = None
        use_web_socket = False
        running_in_windows = False
        auth_token: str | None = None

        try:
            parsed_content = json_parse(content)
            if not isinstance(parsed_content, dict):
                raise ValueError("not an object")
            if parsed_content.get("workspaceFolders"):
                workspace_folders = parsed_content["workspaceFolders"]
            pid = parsed_content.get("pid")
            ide_name = parsed_content.get("ideName")
            use_web_socket = parsed_content.get("transport") == "ws"
            running_in_windows = parsed_content.get("runningInWindows") is True
            auth_token = parsed_content.get("authToken")
        except Exception:  # noqa: BLE001 - older format: just a list of paths
            workspace_folders = [line.strip() for line in content.split("\n")]

        # Extract the port from the filename (e.g. 12345.lock -> 12345).
        filename = path.split(PATH_SEPARATOR)[-1] if path.split(PATH_SEPARATOR) else None
        if not filename:
            return None

        port = filename.replace(".lock", "")

        return IdeLockfileInfo(
            workspace_folders=workspace_folders,
            port=int(port),
            pid=pid,
            ide_name=ide_name,
            use_web_socket=use_web_socket,
            running_in_windows=running_in_windows,
            auth_token=auth_token,
        )
    except Exception as error:  # noqa: BLE001
        log_error(error)
        return None


async def check_ide_connection(host: str, port: int, timeout: float = 500) -> bool:
    """Whether the IDE port is open (TCP connect probe). ``timeout`` is in milliseconds."""
    try:
        loop = asyncio.get_running_loop()

        async def _probe() -> bool:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                await loop.sock_connect(sock, (host, port))
                return True
            except Exception:  # noqa: BLE001 - connection refused / error
                return False
            finally:
                sock.close()

        return await asyncio.wait_for(_probe(), timeout=timeout / 1000)
    except Exception:  # noqa: BLE001 - timeout, invalid host, etc.
        return False


async def _get_windows_user_profile_impl() -> str | None:
    """Resolve the Windows USERPROFILE path. WSL often doesn't pass USERPROFILE through, so fall
    back to shelling out to powershell.exe (~500ms–2s cold; static per session)."""
    if os.environ.get("USERPROFILE"):
        return os.environ["USERPROFILE"]
    result = await exec_file_no_throw(
        "powershell.exe",
        ["-NoProfile", "-NonInteractive", "-Command", "$env:USERPROFILE"],
    )
    stdout = result.get("stdout") or ""
    if result.get("code") == 0 and stdout.strip():
        return stdout.strip()
    log_for_debugging(
        "Unable to get Windows USERPROFILE via PowerShell - IDE detection may be incomplete"
    )
    return None


get_windows_user_profile = memoize_with_ttl_async(_get_windows_user_profile_impl)


async def get_ide_lockfiles_paths() -> list[str]:
    """Gets the potential IDE lockfile directories per platform.

    Paths are not pre-checked for existence — the consumer readdirs each and handles ENOENT.
    """
    paths: list[str] = [join(get_tabvis_config_home_dir(), "ide")]

    if get_platform() != "wsl":
        return paths

    # For Windows under WSL, use heuristics to find the potential paths.
    windows_home = await get_windows_user_profile()

    if windows_home:
        converter = WindowsToWSLConverter(os.environ.get("WSL_DISTRO_NAME"))
        wsl_path = converter.to_local_path(windows_home)
        paths.append(os.path.realpath(join(wsl_path, ".tabvis", "ide")))

    # Construct the path based on the standard Windows WSL locations. This can fail if the current
    # user does not have "List folder contents" permission on C:\\Users.
    try:
        users_dir = "/mnt/c/Users"
        user_dirs = await get_fs_implementation().readdir(users_dir)

        for user in user_dirs:
            # Skip files (e.g. desktop.ini) — readdir on a file path throws ENOTDIR.
            if not user.is_directory() and not user.is_symbolic_link():
                continue
            if user.name in ("Public", "Default", "Default User", "All Users"):
                continue  # Skip system directories
            paths.append(join(users_dir, user.name, ".tabvis", "ide"))
    except Exception as error:  # noqa: BLE001
        if _is_fs_inaccessible(error):
            log_for_debugging(
                f"WSL IDE lockfile path detection failed ({get_errno_code(error)}): "
                f"{get_error_message(error)}"
            )
        else:
            log_error(error)
    return paths


async def cleanup_stale_ide_lockfiles() -> None:
    """Removes lockfiles for dead processes / non-responding ports."""
    try:
        lockfiles = await get_sorted_ide_lockfiles()

        for lockfile_path in lockfiles:
            lockfile_info = await read_ide_lockfile(lockfile_path)

            if not lockfile_info:
                # If we can't read the lockfile, delete it.
                try:
                    await get_fs_implementation().unlink(lockfile_path)
                except Exception as error:  # noqa: BLE001
                    log_error(error)
                continue

            host = await detect_host_ip(lockfile_info.running_in_windows, lockfile_info.port)

            should_delete = False

            if lockfile_info.pid:
                # Check if the process is still running.
                if not is_process_running(lockfile_info.pid):
                    if get_platform() != "wsl":
                        should_delete = True
                    else:
                        # The PID may not be reliable in WSL, so also check the connection.
                        is_responding = await check_ide_connection(host, lockfile_info.port)
                        if not is_responding:
                            should_delete = True
            else:
                # No PID, check if the URL is responding.
                is_responding = await check_ide_connection(host, lockfile_info.port)
                if not is_responding:
                    should_delete = True

            if should_delete:
                try:
                    await get_fs_implementation().unlink(lockfile_path)
                except Exception as error:  # noqa: BLE001
                    log_error(error)
    except Exception as error:  # noqa: BLE001
        log_error(error)


@dataclass
class IDEExtensionInstallationStatus:
    installed: bool
    error: str | None
    installed_version: str | None
    ide_type: IdeType | None


async def maybe_install_ide_extension(
    ide_type: IdeType,
) -> IDEExtensionInstallationStatus | None:
    try:
        # Install/update the extension.
        installed_version = await install_ide_extension(ide_type)

        # Set diff tool config to auto if it has not been set already.
        global_config = _get_global_config()
        if not global_config.get("diffTool"):
            _save_global_config(lambda current: {**current, "diffTool": "auto"})
        return IDEExtensionInstallationStatus(
            installed=True,
            error=None,
            installed_version=installed_version,
            ide_type=ide_type,
        )
    except Exception as error:  # noqa: BLE001
        error_message = str(error)
        log_error(error)
        return IDEExtensionInstallationStatus(
            installed=False,
            error=error_message,
            installed_version=None,
            ide_type=ide_type,
        )


_current_ide_search: Any = None


async def find_available_ide() -> DetectedIDEInfo | None:
    global _current_ide_search
    if _current_ide_search is not None:
        _current_ide_search.abort()
    _current_ide_search = create_abort_controller()
    signal = _current_ide_search.signal

    # Clean up stale IDE lockfiles first so we don't check them at all.
    await cleanup_stale_ide_lockfiles()
    start_time = _now_ms()
    while _now_ms() - start_time < 30_000 and not signal.aborted:
        # Skip iteration during scroll drain — detect_ides reads lockfiles + shells out to ps,
        # competing for the event loop with scroll frames.
        if get_is_scroll_draining():
            await sleep(1000, signal)
            continue
        ides = await detect_ides(False)
        if signal.aborted:
            return None
        # Return the IDE iff there is exactly one match, otherwise the user must use /ide.
        if len(ides) == 1:
            return ides[0]
        await sleep(1000, signal)
    return None


async def detect_ides(include_invalid: bool) -> list[DetectedIDEInfo]:
    """Detects IDEs that have a running extension/plugin.

    ``include_invalid``: if True, also return IDEs whose workspace dir doesn't match the cwd.
    """
    detected_ides: list[DetectedIDEInfo] = []

    try:
        # Get the TABVIS_SSE_PORT if set.
        sse_port = os.environ.get("TABVIS_SSE_PORT")
        env_port = int(sse_port) if sse_port else None

        # Get the cwd, normalized to NFC for consistent comparison. macOS returns NFD paths;
        # IDEs like VS Code report NFC. Without normalization, accented/CJK paths fail to match.
        import unicodedata

        cwd = unicodedata.normalize("NFC", get_original_cwd())

        # Get sorted lockfiles (full paths) and read them all in parallel.
        lockfiles = await get_sorted_ide_lockfiles()
        lockfile_infos = await asyncio.gather(*[read_ide_lockfile(p) for p in lockfiles])

        # Ancestor PID walk shells out. Make it lazy and single-shot per detect_ides() call.
        get_ancestors = _make_ancestor_pid_lookup()
        needs_ancestry_check = get_platform() != "wsl" and is_supported_terminal()

        # Try to find a lockfile that contains our current working directory.
        for lockfile_info in lockfile_infos:
            if not lockfile_info:
                continue

            is_valid = False
            if is_env_truthy(os.environ.get("TABVIS_IDE_SKIP_VALID_CHECK")):
                is_valid = True
            elif lockfile_info.port == env_port:
                # If the port matches the env var, mark valid regardless of directory.
                is_valid = True
            else:
                is_valid = _workspace_contains_cwd(lockfile_info, cwd)

            if not is_valid and not include_invalid:
                continue

            # PID ancestry check: when running in a supported IDE's built-in terminal, ensure this
            # lockfile's IDE is actually our parent process. Runs AFTER the workspace check.
            if needs_ancestry_check:
                port_matches_env = env_port is not None and lockfile_info.port == env_port
                if not port_matches_env:
                    if not lockfile_info.pid or not is_process_running(lockfile_info.pid):
                        continue
                    if os.getppid() != lockfile_info.pid:
                        ancestors = await get_ancestors()
                        if lockfile_info.pid not in ancestors:
                            continue

            ide_name = lockfile_info.ide_name or (
                to_ide_display_name(env_dynamic.terminal) if is_supported_terminal() else "IDE"
            )

            host = await detect_host_ip(lockfile_info.running_in_windows, lockfile_info.port)
            if lockfile_info.use_web_socket:
                url = f"ws://{host}:{lockfile_info.port}"
            else:
                url = f"http://{host}:{lockfile_info.port}/sse"

            detected_ides.append(
                DetectedIDEInfo(
                    url=url,
                    name=ide_name,
                    workspace_folders=lockfile_info.workspace_folders,
                    port=lockfile_info.port,
                    is_valid=is_valid,
                    auth_token=lockfile_info.auth_token,
                    ide_running_in_windows=lockfile_info.running_in_windows,
                )
            )

        # The env_port should be defined for supported IDE terminals. If there is an extension
        # with a matching env_port, single that one out and return it; otherwise return all valid.
        if not include_invalid and env_port:
            env_port_match = [
                ide for ide in detected_ides if ide.is_valid and ide.port == env_port
            ]
            if len(env_port_match) == 1:
                return env_port_match
    except Exception as error:  # noqa: BLE001
        log_error(error)

    return detected_ides


def _workspace_contains_cwd(lockfile_info: IdeLockfileInfo, cwd: str) -> bool:
    """Check if ``cwd`` is within any of the lockfile's workspace folders."""
    import unicodedata

    for ide_path in lockfile_info.workspace_folders:
        if not ide_path:
            continue

        local_path = ide_path

        # Handle WSL-specific path conversion and distro matching.
        if (
            get_platform() == "wsl"
            and lockfile_info.running_in_windows
            and os.environ.get("WSL_DISTRO_NAME")
        ):
            if not check_wsl_distro_match(ide_path, os.environ["WSL_DISTRO_NAME"]):
                continue

            # Try both the original path and the converted path.
            resolved_original = unicodedata.normalize("NFC", os.path.realpath(local_path))
            if cwd == resolved_original or cwd.startswith(resolved_original + PATH_SEPARATOR):
                return True

            converter = WindowsToWSLConverter(os.environ["WSL_DISTRO_NAME"])
            local_path = converter.to_local_path(ide_path)

        resolved_path = unicodedata.normalize("NFC", os.path.realpath(local_path))

        # On Windows, normalize for case-insensitive drive-letter comparison.
        if get_platform() == "windows":
            normalized_cwd = _uppercase_drive_letter(cwd)
            normalized_resolved_path = _uppercase_drive_letter(resolved_path)
            if normalized_cwd == normalized_resolved_path or normalized_cwd.startswith(
                normalized_resolved_path + PATH_SEPARATOR
            ):
                return True
            continue

        if cwd == resolved_path or cwd.startswith(resolved_path + PATH_SEPARATOR):
            return True
    return False


def _uppercase_drive_letter(path: str) -> str:
    """Uppercase a leading ``x:`` drive letter (parity with TS ``/^[a-zA-Z]:/`` replace)."""
    if len(path) >= 2 and path[0].isascii() and path[0].isalpha() and path[1] == ":":
        return path[0].upper() + path[1:]
    return path


async def maybe_notify_ide_connected(client: Any) -> None:
    await client.notification(
        {
            "method": "ide_connected",
            "params": {"pid": os.getpid()},
        }
    )


def has_access_to_ide_extension_diff_feature(
    mcp_clients: list[MCPServerConnection],
) -> bool:
    """Whether there's a connected IDE client in the provided MCP clients list."""
    return any(
        getattr(client, "type", None) == "connected" and getattr(client, "name", None) == "ide"
        for client in mcp_clients
    )


EXTENSION_ID = "tabvis.agent-core"


async def is_ide_extension_installed(ide_type: IdeType) -> bool:
    if is_vscode_ide(ide_type):
        command = await get_vscode_ide_command(ide_type)
        if command:
            try:
                result = await exec_file_no_throw_with_cwd(
                    command,
                    ["--list-extensions"],
                    {"env": _get_installation_env()},
                )
                if EXTENSION_ID in (result.get("stdout") or ""):
                    return True
            except Exception:  # noqa: BLE001 - eat the error
                pass
    elif is_jet_brains_ide(ide_type):
        return await is_jet_brains_plugin_installed_cached(ide_type)
    return False


async def install_ide_extension(ide_type: IdeType) -> str | None:
    if is_vscode_ide(ide_type):
        command = await get_vscode_ide_command(ide_type)

        if command:
            version = await get_installed_vscode_extension_version(command)
            # If not installed or older than the bundled version:
            if not version or lt(version, _get_tabvis_version()):
                # `code` may crash when invoked too quickly in succession.
                await sleep(500)
                result = await exec_file_no_throw_with_cwd(
                    command,
                    ["--force", "--install-extension", EXTENSION_ID],
                    {"env": _get_installation_env()},
                )
                if result.get("code") != 0:
                    raise RuntimeError(
                        f"{result.get('code')}: {result.get('error')} {result.get('stderr')}"
                    )
                version = _get_tabvis_version()
            return version
    # No automatic install for JetBrains IDEs; we show a prominent download notice instead.
    return None


def _get_installation_env() -> dict[str, str] | None:
    # Cursor on Linux may incorrectly implement the `code` command and launch the UI. Make it
    # error out by clearing DISPLAY.
    if get_platform() == "linux":
        return {**os.environ, "DISPLAY": ""}
    return None


def _get_tabvis_version() -> str:
    return MACRO.VERSION


async def get_installed_vscode_extension_version(command: str) -> str | None:
    result = await exec_file_no_throw(
        command,
        ["--list-extensions", "--show-versions"],
        {"env": _get_installation_env()},
    )
    lines = (result.get("stdout") or "").split("\n")
    for line in lines:
        parts = line.split("@")
        extension_id = parts[0] if parts else ""
        version = parts[1] if len(parts) > 1 else ""
        if extension_id == EXTENSION_ID and version:
            return version
    return None


def get_vscode_ide_command_by_parent_process() -> str | None:
    try:
        platform = get_platform()

        # Only supported on macOS, where Cursor can register itself as the 'code' command.
        if platform != "macos":
            return None

        from tabvis.utils.exec_file_no_throw import exec_sync_with_defaults_deprecated

        pid = os.getppid()

        # Walk up the process tree to find the actual app.
        for _ in range(10):
            if not pid or pid in (0, 1):
                break

            # Get the command for this PID (already returned if not macOS).
            command_raw = exec_sync_with_defaults_deprecated(f"ps -o command= -p {pid}")
            command = command_raw.strip() if command_raw else None

            if command:
                # Known applications: extract the path up to and including .app.
                app_names = {
                    "Visual Studio Code.app": "code",
                    "Cursor.app": "cursor",
                    "Windsurf.app": "windsurf",
                    "Visual Studio Code - Insiders.app": "code",
                    "VSCodium.app": "codium",
                }
                path_to_executable = "/Contents/MacOS/Electron"

                for app_name, executable_name in app_names.items():
                    app_index = command.find(app_name + path_to_executable)
                    if app_index != -1:
                        folder_path_end = app_index + len(app_name)
                        return (
                            command[:folder_path_end]
                            + "/Contents/Resources/app/bin/"
                            + executable_name
                        )

            # Get parent PID (already returned if not macOS).
            ppid_str_raw = exec_sync_with_defaults_deprecated(f"ps -o ppid= -p {pid}")
            ppid_str = ppid_str_raw.strip() if ppid_str_raw else None
            if not ppid_str:
                break
            pid = int(ppid_str.strip())

        return None
    except Exception:  # noqa: BLE001
        return None


async def get_vscode_ide_command(ide_type: IdeType) -> str | None:
    parent_executable = get_vscode_ide_command_by_parent_process()
    if parent_executable:
        # Verify the parent executable actually exists.
        try:
            await get_fs_implementation().stat(parent_executable)
            return parent_executable
        except Exception:  # noqa: BLE001 - parent executable doesn't exist
            pass

    # On Windows, explicitly request the .cmd wrapper (see TS comment re: VS Code 1.110.0).
    ext = ".cmd" if get_platform() == "windows" else ""
    if ide_type == "vscode":
        return "code" + ext
    if ide_type == "cursor":
        return "cursor" + ext
    if ide_type == "windsurf":
        return "windsurf" + ext
    return None


async def is_cursor_installed() -> bool:
    result = await exec_file_no_throw("cursor", ["--version"])
    return result.get("code") == 0


async def is_windsurf_installed() -> bool:
    result = await exec_file_no_throw("windsurf", ["--version"])
    return result.get("code") == 0


async def is_vscode_installed() -> bool:
    result = await exec_file_no_throw("code", ["--help"])
    # Check if the output indicates this is actually Visual Studio Code.
    return result.get("code") == 0 and "Visual Studio Code" in (result.get("stdout") or "")


# Cache for IDE detection results.
_cached_running_ides: list[IdeType] | None = None


async def _detect_running_ides_impl() -> list[IdeType]:
    """Internal implementation of IDE detection (shells out to ps/tasklist)."""
    running_ides: list[IdeType] = []

    try:
        platform = get_platform()
        if platform == "macos":
            result = await _exec_shell(
                'ps aux | grep -E "Visual Studio Code|Code Helper|Cursor Helper|Windsurf Helper'
                "|IntelliJ IDEA|PyCharm|WebStorm|PhpStorm|RubyMine|CLion|GoLand|Rider|DataGrip"
                '|AppCode|DataSpell|Aqua|Gateway|Fleet|Android Studio" | grep -v grep'
            )
            stdout = result or ""
            for ide, config in SUPPORTED_IDE_CONFIGS.items():
                for keyword in config.process_keywords_mac:
                    if keyword in stdout:
                        running_ides.append(ide)  # type: ignore[arg-type]
                        break
        elif platform == "windows":
            result = await _exec_shell(
                'tasklist | findstr /I "Code.exe Cursor.exe Windsurf.exe idea64.exe pycharm64.exe '
                "webstorm64.exe phpstorm64.exe rubymine64.exe clion64.exe goland64.exe rider64.exe "
                'datagrip64.exe appcode.exe dataspell64.exe aqua64.exe gateway64.exe fleet.exe '
                'studio64.exe"'
            )
            normalized_stdout = (result or "").lower()
            for ide, config in SUPPORTED_IDE_CONFIGS.items():
                for keyword in config.process_keywords_windows:
                    if keyword.lower() in normalized_stdout:
                        running_ides.append(ide)  # type: ignore[arg-type]
                        break
        elif platform == "linux":
            result = await _exec_shell(
                'ps aux | grep -E "code|cursor|windsurf|idea|pycharm|webstorm|phpstorm|rubymine'
                '|clion|goland|rider|datagrip|dataspell|aqua|gateway|fleet|android-studio" '
                "| grep -v grep"
            )
            normalized_stdout = (result or "").lower()
            for ide, config in SUPPORTED_IDE_CONFIGS.items():
                for keyword in config.process_keywords_linux:
                    if keyword in normalized_stdout:
                        if ide != "vscode":
                            running_ides.append(ide)  # type: ignore[arg-type]
                            break
                        if (
                            "cursor" not in normalized_stdout
                            and "appcode" not in normalized_stdout
                        ):
                            # Special case conflicting keywords from some IDEs.
                            running_ides.append(ide)  # type: ignore[arg-type]
                            break
    except Exception as error:  # noqa: BLE001 - if detection fails, return empty
        log_error(error)

    return running_ides


async def _exec_shell(command: str) -> str | None:
    """execa(cmd, {shell: True, reject: False}) → stdout (never throws)."""
    result = await exec_file_no_throw_with_cwd(command, [], {"shell": True})
    return result.get("stdout")


async def detect_running_ides() -> list[IdeType]:
    """Fresh detection (~150ms); updates the cache for detect_running_ides_cached()."""
    global _cached_running_ides
    result = await _detect_running_ides_impl()
    _cached_running_ides = result
    return result


async def detect_running_ides_cached() -> list[IdeType]:
    """Cached IDE detection results, or fresh detection if cache is empty."""
    if _cached_running_ides is None:
        return await detect_running_ides()
    return _cached_running_ides


def reset_detect_running_ides() -> None:
    """Resets the detect_running_ides_cached cache (exported for testing)."""
    global _cached_running_ides
    _cached_running_ides = None


def get_connected_ide_name(mcp_clients: list[MCPServerConnection]) -> str | None:
    ide_client = next(
        (
            c
            for c in mcp_clients
            if getattr(c, "type", None) == "connected" and getattr(c, "name", None) == "ide"
        ),
        None,
    )
    return get_ide_client_name(ide_client)


def get_ide_client_name(ide_client: MCPServerConnection | None = None) -> str | None:
    config = getattr(ide_client, "config", None)
    config_type = getattr(getattr(config, "config", config), "type", None)
    if config_type in ("sse-ide", "ws-ide"):
        return getattr(getattr(config, "config", config), "ide_name", None)
    return to_ide_display_name(env_dynamic.terminal) if is_supported_terminal() else None


EDITOR_DISPLAY_NAMES: dict[str, str] = {
    "code": "VS Code",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "antigravity": "Antigravity",
    "vi": "Vim",
    "vim": "Vim",
    "nano": "nano",
    "notepad": "Notepad",
    "start /wait notepad": "Notepad",
    "emacs": "Emacs",
    "subl": "Sublime Text",
    "atom": "Atom",
}


def to_ide_display_name(terminal: str | None) -> str:
    if not terminal:
        return "IDE"

    config = SUPPORTED_IDE_CONFIGS.get(terminal)
    if config:
        return config.display_name

    # Check editor command names (exact match first).
    editor_name = EDITOR_DISPLAY_NAMES.get(terminal.lower().strip())
    if editor_name:
        return editor_name

    # Extract command name from path/arguments (e.g. "/usr/bin/code --wait" -> "code").
    parts = terminal.split(" ")
    command = parts[0] if parts else ""
    command_name = basename(command).lower() if command else None
    if command_name:
        mapped_name = EDITOR_DISPLAY_NAMES.get(command_name)
        if mapped_name:
            return mapped_name
        # Fallback: capitalize the command basename.
        return _capitalize(command_name)

    # Fallback: capitalize first letter.
    return _capitalize(terminal)


def _capitalize(value: str) -> str:
    """Uppercase the first character and lowercase the remainder."""
    if not value:
        return value
    return value[0].upper() + value[1:].lower()


def get_connected_ide_client(
    mcp_clients: list[MCPServerConnection] | None = None,
) -> ConnectedMCPServer | None:
    """Gets the connected IDE client from a list of MCP clients."""
    if not mcp_clients:
        return None

    ide_client = next(
        (
            c
            for c in mcp_clients
            if getattr(c, "type", None) == "connected" and getattr(c, "name", None) == "ide"
        ),
        None,
    )

    # Type guard to ensure we return the correct type.
    return ide_client if getattr(ide_client, "type", None) == "connected" else None


async def close_open_diffs(ide_client: ConnectedMCPServer) -> None:
    """Notifies the IDE that a new prompt has been submitted (closes all diff tabs)."""
    try:
        await call_ide_rpc("closeAllDiffTabs", {}, ide_client)
    except Exception:  # noqa: BLE001 - silently ignore (IDE may not support this)
        pass


async def initialize_ide_integration(
    on_ide_detected: Any,
    ide_to_install_extension: IdeType | None,
    on_show_ide_onboarding: Any,
    on_installation_complete: Any,
) -> None:
    """Initializes IDE detection + extension installation, then invokes the callbacks."""

    # Don't await so we don't block startup.
    async def _detect_and_notify() -> None:
        on_ide_detected(await find_available_ide())

    asyncio.ensure_future(_detect_and_notify())

    should_auto_install = _get_global_config().get("autoInstallIdeExtension", True)
    if should_auto_install is None:
        should_auto_install = True
    if (
        not is_env_truthy(os.environ.get("TABVIS_IDE_SKIP_AUTO_INSTALL"))
        and should_auto_install
    ):
        ide_type = ide_to_install_extension or get_terminal_ide_type()
        if ide_type:
            if is_vscode_ide(ide_type):

                async def _vscode_flow() -> None:
                    await is_ide_extension_installed(ide_type)
                    try:
                        status = await maybe_install_ide_extension(ide_type)
                    except Exception as error:  # noqa: BLE001
                        status = IDEExtensionInstallationStatus(
                            installed=False,
                            error=str(error) or "Installation failed",
                            installed_version=None,
                            ide_type=ide_type,
                        )
                    on_installation_complete(status)
                    if status and status.installed:
                        # If we installed and don't yet have an IDE, search again.
                        async def _re_detect() -> None:
                            on_ide_detected(await find_available_ide())

                        asyncio.ensure_future(_re_detect())
                    # Dead branch in TS (guarded by `false`); preserved as no-op.

                asyncio.ensure_future(_vscode_flow())
            elif is_jet_brains_ide(ide_type):
                # Always check installation to populate the sync cache used by status notices.
                async def _jetbrains_flow() -> None:
                    await is_ide_extension_installed(ide_type)
                    # Dead branch in TS (guarded by `false`); preserved as no-op.

                asyncio.ensure_future(_jetbrains_flow())


async def _detect_host_ip_impl(is_ide_running_in_windows: bool, port: int) -> str:
    """Detects the host IP to use to connect to the extension."""
    if os.environ.get("TABVIS_IDE_HOST_OVERRIDE"):
        return os.environ["TABVIS_IDE_HOST_OVERRIDE"]

    if get_platform() != "wsl" or not is_ide_running_in_windows:
        return "127.0.0.1"

    # Under WSL2 with the extension running in Windows, we must use a different IP.
    try:
        route_result = await exec_file_no_throw_with_cwd(
            "ip route show | grep -i default", [], {"shell": True}
        )
        if route_result.get("code") == 0 and route_result.get("stdout"):
            import re

            gateway_match = re.search(
                r"default via (\d+\.\d+\.\d+\.\d+)", route_result["stdout"]
            )
            if gateway_match:
                gateway_ip = gateway_match.group(1)
                if await check_ide_connection(gateway_ip, port):
                    return gateway_ip
    except Exception:  # noqa: BLE001 - suppress any errors
        pass

    # Fallback to the default if we cannot find anything.
    return "127.0.0.1"


detect_host_ip = memoize_with_ttl_async(_detect_host_ip_impl)


async def install_from_artifactory(command: str) -> str:
    """ANT-only: download + install the bundled VS Code .vsix from artifactory.

    Uses stdlib ``urllib`` for the version fetch and .vsix stream download. This path is gated on
    ``USER_TYPE == 'ant'`` and is dead in the external build.
    """
    import urllib.request

    # Read auth token from ~/.npmrc.
    npmrc_path = join(os.path.expanduser("~"), ".npmrc")
    auth_token: str | None = None
    fs = get_fs_implementation()

    try:
        npmrc_content = await fs.read_file(npmrc_path, {"encoding": "utf8"})
        import re

        lines = npmrc_content.split("\n")
        for line in lines:
            match = re.search(
                r"//artifactory\.infra\.ant\.dev/artifactory/api/npm/npm-all/:_authToken=(.+)",
                line,
            )
            if match and match.group(1):
                auth_token = match.group(1).strip()
                break
    except Exception as error:  # noqa: BLE001
        log_error(error)
        raise RuntimeError(f"Failed to read npm authentication: {error}") from error

    if not auth_token:
        raise RuntimeError("No artifactory auth token found in ~/.npmrc")

    version_url = (
        "https://artifactory.infra.ant.dev/artifactory/armorcode-tabvis-internal/"
        "tabvis-vscode-releases/stable"
    )

    try:
        req = urllib.request.Request(  # noqa: S310 - https URL, ANT-only path
            version_url, headers={"Authorization": f"Bearer {auth_token}"}
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            version = resp.read().decode().strip()
        if not version:
            raise RuntimeError("No version found in artifactory response")

        import time

        vsix_url = (
            "https://artifactory.infra.ant.dev/artifactory/armorcode-tabvis-internal/"
            f"tabvis-vscode-releases/{version}/tabvis.vsix"
        )
        import tempfile

        temp_vsix_path = join(
            tempfile.gettempdir(), f"tabvis-{version}-{int(time.time() * 1000)}.vsix"
        )

        try:
            vsix_req = urllib.request.Request(  # noqa: S310
                vsix_url, headers={"Authorization": f"Bearer {auth_token}"}
            )
            with urllib.request.urlopen(vsix_req) as vsix_resp:  # noqa: S310
                data = vsix_resp.read()
            await asyncio.to_thread(_write_bytes, temp_vsix_path, data)

            # Add delay to prevent code command crashes.
            await sleep(500)

            result = await exec_file_no_throw_with_cwd(
                command,
                ["--force", "--install-extension", temp_vsix_path],
                {"env": _get_installation_env()},
            )

            if result.get("code") != 0:
                raise RuntimeError(
                    f"{result.get('code')}: {result.get('error')} {result.get('stderr')}"
                )

            return version
        finally:
            try:
                await fs.unlink(temp_vsix_path)
            except Exception:  # noqa: BLE001 - ignore cleanup errors
                pass
    except RuntimeError:
        raise
    except Exception as error:  # noqa: BLE001 - axios.isAxiosError analogue
        raise RuntimeError(
            f"Failed to fetch extension version from artifactory: {error}"
        ) from error


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


# --- Helpers for unported deps ----------------------------------------------------------------


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _is_fs_inaccessible(error: Any) -> bool:
    """Whether ``error`` is an expected missing/inaccessible-fs error (ENOENT/ENOTDIR/EACCES…)."""
    code = get_errno_code(error)
    return code in ("ENOENT", "ENOTDIR", "EACCES", "EPERM", "ENOTCONN")


# (config.py only carries the enable/are-enabled gate). The IDE extension installer only needs
# the global ``diffTool`` / ``autoInstallIdeExtension`` keys, so fall back to reading/writing the
# global ``.tabvis.json`` file directly. Replace with tabvis.utils.config once that lands.
def _get_global_config() -> dict[str, Any]:
    try:
        from tabvis.utils.config import get_global_config  # type: ignore[attr-defined]

        return get_global_config()
    except Exception:  # noqa: BLE001 - not implemented yet, fall back to disk
        pass
    path = _global_config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json_parse(fh.read())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - missing / unreadable
        return {}


def _save_global_config(updater: Any) -> None:
    try:
        from tabvis.utils.config import save_global_config  # type: ignore[attr-defined]

        save_global_config(updater)
        return
    except Exception:  # noqa: BLE001 - not implemented yet, fall back to disk
        pass
    import json

    current = _get_global_config()
    new_config = updater(current)
    path = _global_config_path()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(new_config, fh)
    except Exception as error:  # noqa: BLE001
        log_error(error)


def _global_config_path() -> str:
    return join(os.environ.get("TABVIS_CONFIG_DIR") or os.path.expanduser("~"), ".tabvis.json")
