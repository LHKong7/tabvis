r"""Path conversion for IDE communication

Handles conversions between Tabvis's environment and the IDE's environment (the Windows-IDE +
WSL-Tabvis scenario). The TS exports an ``IDEPathConverter`` interface, a ``WindowsToWSLConverter``
class implementing it via ``wslpath`` (with a manual fallback), and a ``checkWSLDistroMatch``
predicate.

Casing: Python identifiers are snake_case, the class is PascalCase. The interface methods
``toLocalPath``/``toIDEPath`` become ``to_local_path``/``to_ide_path``. All values are plain
``str`` paths — no wire-key dicts.

Faithful-behavior notes:
- ``execFileSync('wslpath', ['-u'|'-w', path])`` → ``subprocess.run(['wslpath', ...])`` with
  stderr discarded (TS: ``stdio: ['pipe', 'pipe', 'ignore']`` because wslpath writes
  ``wslpath: <err>`` to stderr), output ``.strip()``-ed. Any failure (missing binary,
  non-zero exit) falls back exactly as the TS ``catch`` does.
- The WSL UNC regex ``^\\\\wsl(?:\.localhost|\$)\\([^\\]+)(.*)$`` is kept behaviorally equivalent; group 1
  is the distro name, group 2 the trailing path.
- Falsy guard ``if (!windowsPath) return windowsPath`` → ``if not windows_path``: empty string
  returns the (empty) input unchanged, matching JS.
- ``IDEPathConverter`` is an ABC so subclasses are checkable; the TS interface is structural,
  but an ABC is the faithful Python analogue of "implements IDEPathConverter".
"""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod

# ^\\wsl(?:\.localhost|$)\<distro>(<rest>)$  — a WSL UNC path. Note the literal "$" alternative
# (the ``\\wsl$\`` legacy form) is the TS ``\$`` inside the regex, not end-of-string.
_WSL_UNC_RE = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)(.*)$")

# Drive-letter prefix: ^([A-Z]): case-insensitive, used by the manual fallback.
_DRIVE_LETTER_RE = re.compile(r"^([A-Za-z]):")


class IDEPathConverter(ABC):
    """Convert paths between Tabvis's local format and the IDE's format."""

    @abstractmethod
    def to_local_path(self, ide_path: str) -> str:
        """Convert a path from IDE format to Tabvis's local format.

        Used when reading workspace folders from the IDE lockfile.
        """

    @abstractmethod
    def to_ide_path(self, local_path: str) -> str:
        """Convert a path from Tabvis's local format to IDE format.

        Used when sending paths to the IDE (showDiffInIDE, etc.).
        """


class WindowsToWSLConverter(IDEPathConverter):
    """Converter for the Windows-IDE + WSL-Tabvis scenario."""

    def __init__(self, wsl_distro_name: str | None) -> None:
        self._wsl_distro_name = wsl_distro_name

    def to_local_path(self, windows_path: str) -> str:
        if not windows_path:
            return windows_path

        # Check if this is a path from a different WSL distro.
        if self._wsl_distro_name:
            wsl_unc_match = _WSL_UNC_RE.match(windows_path)
            if wsl_unc_match and wsl_unc_match.group(1) != self._wsl_distro_name:
                # Different distro - wslpath will fail, so return the original path.
                return windows_path

        try:
            # Use wslpath to convert Windows paths to WSL paths.
            result = subprocess.run(
                ["wslpath", "-u", windows_path],
                capture_output=True,
                text=True,
                check=True,
                stdin=subprocess.DEVNULL,
            )
            return result.stdout.strip()
        except Exception:  # noqa: BLE001 - parity with the TS catch; any failure → fallback
            # If wslpath fails, fall back to manual conversion:
            #   convert backslashes to forward slashes, then C: → /mnt/c.
            converted = windows_path.replace("\\", "/")
            return _DRIVE_LETTER_RE.sub(
                lambda m: f"/mnt/{m.group(1).lower()}", converted
            )

    def to_ide_path(self, wsl_path: str) -> str:
        if not wsl_path:
            return wsl_path

        try:
            # Use wslpath to convert WSL paths to Windows paths.
            result = subprocess.run(
                ["wslpath", "-w", wsl_path],
                capture_output=True,
                text=True,
                check=True,
                stdin=subprocess.DEVNULL,
            )
            return result.stdout.strip()
        except Exception:  # noqa: BLE001 - parity with the TS catch
            # If wslpath fails, return the original path.
            return wsl_path


def check_wsl_distro_match(windows_path: str, wsl_distro_name: str) -> bool:
    """Whether ``windows_path``'s distro matches ``wsl_distro_name`` for WSL UNC paths.

    Returns ``True`` for non-UNC paths (no distro mismatch possible).
    """
    wsl_unc_match = _WSL_UNC_RE.match(windows_path)
    if wsl_unc_match:
        return wsl_unc_match.group(1) == wsl_distro_name
    # Not a WSL UNC path, so no distro mismatch.
    return True
