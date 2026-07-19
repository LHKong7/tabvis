"""Shared constants + path builders for the MDM settings modules.

Has ZERO heavy imports (only ``os`` / ``getpass``)
so it is safe to use from :mod:`tabvis.utils.settings.mdm.raw_read`. Both ``raw_read`` and the
(not-yet-implemented) ``mdm/settings`` consumer import from here to avoid duplication.

Casing: per the naming conventions, ``tabvis/utils/settings`` consts keep their TS UPPER_CASE names
verbatim; the registry/path string literals round-trip to the OS so they are kept byte-for-byte.
"""

from __future__ import annotations


# macOS preference domain for Tabvis MDM profiles.
MACOS_PREFERENCE_DOMAIN = "com.tabvis"

# Windows registry key paths for Tabvis MDM policies.
#
# These keys live under SOFTWARE\Policies which is on the WOW64 shared key list — both 32-bit and
# 64-bit processes see the same values without redirection. Do not move these to SOFTWARE\Tabvis,
# as SOFTWARE is redirected and 32-bit processes would silently read from WOW6432Node.
# See: https://learn.microsoft.com/en-us/windows/win32/winprog64/shared-registry-keys
WINDOWS_REGISTRY_KEY_PATH_HKLM = "HKLM\\SOFTWARE\\Policies\\Tabvis"
WINDOWS_REGISTRY_KEY_PATH_HKCU = "HKCU\\SOFTWARE\\Policies\\Tabvis"

# Windows registry value name containing the JSON settings blob.
WINDOWS_REGISTRY_VALUE_NAME = "Settings"

# Path to macOS plutil binary.
PLUTIL_PATH = "/usr/bin/plutil"

# Arguments for plutil to convert plist to JSON on stdout (append plist path).
PLUTIL_ARGS_PREFIX: tuple[str, ...] = ("-convert", "json", "-o", "-", "--")

# Subprocess timeout in milliseconds.
MDM_SUBPROCESS_TIMEOUT_MS = 5000


def _user_info_username() -> str:
    """Best-effort current username.

    Returns ``""`` if the username cannot be resolved (mirrors the TS ``try { ... } catch {}``).
    """
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 — match the TS swallow-and-ignore.
        return ""


def get_macos_plist_paths() -> list[dict[str, str]]:
    """macOS plist paths in priority order, highest first.

    Each entry is ``{"path": str, "label": str}``. Evaluates ``USER_TYPE`` at call time so the
    tabvis-only user-writable preferences path is included only in ant builds.
    """
    username = _user_info_username()

    paths: list[dict[str, str]] = []

    if username:
        paths.append(
            {
                "path": f"/Library/Managed Preferences/{username}/{MACOS_PREFERENCE_DOMAIN}.plist",
                "label": "per-user managed preferences",
            }
        )

    paths.append(
        {
            "path": f"/Library/Managed Preferences/{MACOS_PREFERENCE_DOMAIN}.plist",
            "label": "device-level managed preferences",
        }
    )

    return paths
