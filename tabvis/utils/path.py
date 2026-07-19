"""Path expansion / relativization helpers

The TS module wraps Node's ``path`` builtins (``isAbsolute``/``join``/``normalize``/
``relative``/``resolve``/``dirname``) plus ``os.homedir`` and a couple of project utils
(``getCwd``, ``getPlatform``, ``posixPathToWindowsPath``, ``sanitizePath``). This is the
minimal surface the file & glob tools consume: tilde expansion + absolute/relative
resolution, NFC normalization, the cwd-relativizer, the directory-of-path helper, the
traversal check, the config-key normalizer, and ``sanitize_path``.

Casing: Python identifiers are snake_case; this module returns/accepts plain ``str`` paths
(native separators for the current platform), so there are no wire-key dicts to preserve.

Platform note: ``docs/SPINE_CONTRACTS.md`` locks supported platforms to macos/wsl (POSIX).
The TS Windows branch (``getPlatform() === 'windows'`` → ``posixPathToWindowsPath``) is a
no-op here; ``os.path`` already gives native semantics for the running platform.
"""

from __future__ import annotations

import os
import os.path as _osp
import unicodedata

try:  # pragma: no cover - exercised once cwd.py exists
    from tabvis.utils.cwd import get_cwd as _get_cwd
except Exception:  # noqa: BLE001 - module not implemented yet

    def _get_cwd() -> str:
        return os.getcwd()


def _nfc(value: str) -> str:
    """Unicode NFC normalization (parity with TS ``String.prototype.normalize('NFC')``)."""
    return unicodedata.normalize("NFC", value)


def expand_path(path: str, base_dir: str | None = None) -> str:
    """Expand a path that may contain tilde (``~``) notation to an absolute path.

    - ``~``        → the user's home directory.
    - ``~/path``   → ``path`` within the home directory.
    - absolute     → returned normalized.
    - relative     → resolved against ``base_dir`` (defaults to the current cwd).

    The result is NFC-normalized in native separators for the current platform.

    Raises:
        TypeError: if ``path`` or the resolved base dir is not a ``str``.
        ValueError: if either contains a NUL byte (security).
    """
    # Default base dir to the current working directory when not supplied.
    actual_base_dir = base_dir if base_dir is not None else _get_cwd()

    # Input validation (TS throws TypeError for non-string inputs).
    if not isinstance(path, str):
        raise TypeError(f"Path must be a string, received {type(path).__name__}")
    if not isinstance(actual_base_dir, str):
        raise TypeError(
            f"Base directory must be a string, received {type(actual_base_dir).__name__}"
        )

    # Security: reject NUL bytes (TS: "Path contains null bytes").
    if "\0" in path or "\0" in actual_base_dir:
        raise ValueError("Path contains null bytes")

    # Empty / whitespace-only paths resolve to the (normalized) base dir.
    trimmed_path = path.strip()
    if not trimmed_path:
        return _nfc(_osp.normpath(actual_base_dir))

    # Home directory notation.
    if trimmed_path == "~":
        return _nfc(_osp.expanduser("~"))
    if trimmed_path.startswith("~/"):
        return _nfc(_osp.join(_osp.expanduser("~"), trimmed_path[2:]))

    # (Windows POSIX-path conversion intentionally omitted — see module docstring.)
    processed_path = trimmed_path

    # Absolute paths: normalize only.
    if _osp.isabs(processed_path):
        return _nfc(_osp.normpath(processed_path))

    # Relative paths: resolve against the base dir.
    return _nfc(_osp.abspath(_osp.join(actual_base_dir, processed_path)))


def to_relative_path(absolute_path: str) -> str:
    """Relativize ``absolute_path`` against cwd to save tokens in tool output.

    If the path lies outside cwd (the relative form would start with ``..``), the original
    absolute path is returned unchanged so it stays unambiguous.
    """
    relative_path = _osp.relpath(absolute_path, _get_cwd())
    # If the relative path would escape cwd (starts with ..), keep the absolute path.
    return absolute_path if relative_path.startswith("..") else relative_path


def get_directory_for_path(path: str) -> str:
    """Return the directory path for a file or directory path.

    If ``path`` is an existing directory, returns it as-is; otherwise (file or missing),
    returns its parent directory.
    """
    absolute_path = expand_path(path)
    # SECURITY: skip filesystem stats for UNC paths to prevent NTLM credential leaks.
    if absolute_path.startswith("\\\\") or absolute_path.startswith("//"):
        return _osp.dirname(absolute_path)
    try:
        if _osp.isdir(absolute_path):
            return absolute_path
    except OSError:
        # Path can't be accessed — fall through to the parent.
        pass
    return _osp.dirname(absolute_path)


def contains_path_traversal(path: str) -> bool:
    """Whether ``path`` contains a parent-directory traversal segment.

    Matches a ``..`` component bounded by a separator or string boundary on each side
    (e.g. ``../``, ``..\\``, ``a/../b``, or a trailing ``..``). A literal filename like
    ``..foo`` does not match.
    """
    n = len(path)
    i = 0
    while i < n - 1:
        if path[i] == "." and path[i + 1] == ".":
            before_ok = i == 0 or path[i - 1] in "\\/"
            after_idx = i + 2
            after_ok = after_idx >= n or path[after_idx] in "\\/"
            if before_ok and after_ok:
                return True
        i += 1
    return False


def normalize_path_for_config_key(path: str) -> str:
    """Normalize a path for use as a JSON config key (forward slashes, resolved segments)."""
    # Resolve . and .. segments, then force forward slashes for stable JSON keys.
    return _osp.normpath(path).replace("\\", "/")


# --- sanitize_path ------------------------

# Parity with the TS shared zero-dep source: replace non-alphanumerics with '-', and for
# over-long names append a stable short hash so distinct inputs don't collide.
MAX_SANITIZED_LENGTH = 200


def _simple_hash(value: str) -> str:
    """Deterministic base-36 hash (parity with the TS ``simpleHash`` fallback)."""
    # TS simpleHash: h = (h * 31 + charCode) | 0 over a signed 32-bit int, base-36 of |h|.
    h = 0
    for ch in value:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    # Emulate JS `| 0` (interpret as signed 32-bit), then take absolute value.
    if h >= 0x80000000:
        h -= 0x100000000
    h = abs(h)
    if h == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while h:
        h, rem = divmod(h, 36)
        out = digits[rem] + out
    return out


def sanitize_path(name: str) -> str:
    """Sanitize a path/name into an alphanumeric-and-dash token (project-dir keys)."""
    sanitized = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in name)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{_simple_hash(name)}"
