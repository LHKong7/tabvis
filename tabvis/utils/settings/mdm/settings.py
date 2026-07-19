"""MDM (Mobile Device Management) profile enforcement

Reads enterprise settings from OS-level MDM configuration:
- macOS: ``com.tabvis`` preference domain (MDM profiles at ``/Library/Managed Preferences/`` only —
  not user-writable ``~/Library/Preferences/``).
- Windows: ``HKLM\\SOFTWARE\\Policies\\Tabvis`` (admin-only) and ``HKCU\\SOFTWARE\\Policies\\Tabvis``
  (user-writable, lowest priority).
- Linux: no MDM equivalent (uses ``/etc/tabvis/managed-settings.json`` instead).

Policy settings use **first source wins** — the highest-priority source that exists provides all
policy settings. Priority (highest to lowest): remote -> HKLM/plist -> managed-settings.json -> HKCU.

Architecture (this is the parsing / caching / first-source-wins layer):
- :mod:`tabvis.utils.settings.mdm.constants` — shared constants + plist path builder (zero heavy imports).
- :mod:`tabvis.utils.settings.mdm.raw_read` — subprocess I/O only (zero heavy imports).
- this module — parsing, caching, first-source-wins logic.

Zod -> pydantic: the TS ``SettingsSchema().safeParse`` becomes
:meth:`SettingsJson.model_validate` wrapped in a try/except (success/failure mirrors zod's
``{ success, data | error }``). The loose implemented :class:`SettingsJson` accepts more than the strict
TS schema would (see ``validation`` TODO), so failures are rare today.

Casing: Python identifiers snake_case; the returned ``MdmResult`` keeps the wire-ish keys
``settings`` / ``errors`` so it round-trips to the settings pipeline verbatim.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from ...debug import log_for_debugging
from ...diag_logs import log_for_diagnostics_no_pii
from ...file_read import read_file_sync
from ...fs_operations import get_fs_implementation
from ...json import safe_parse_json
from ...startup_profiler import profile_checkpoint
from ..managed_path import get_managed_file_path, get_managed_settings_drop_in_dir
from ..types import SettingsJson
from ..validation import (
    ValidationError,
    filter_invalid_permission_rules,
    format_zod_error,
)
from .constants import (
    WINDOWS_REGISTRY_KEY_PATH_HKCU,
    WINDOWS_REGISTRY_KEY_PATH_HKLM,
    WINDOWS_REGISTRY_VALUE_NAME,
)
from .raw_read import RawReadResult, fire_raw_read, get_mdm_raw_read_promise

# ---------------------------------------------------------------------------
# Types and cache
# ---------------------------------------------------------------------------


@dataclass
class MdmResult:
    """Parsed MDM settings + validation errors."""

    settings: dict[str, Any] = field(default_factory=dict)
    errors: list[ValidationError] = field(default_factory=list)


def _empty_result() -> MdmResult:
    """A fresh empty result (the TS ``EMPTY_RESULT`` is frozen + shared; we return fresh copies)."""
    return MdmResult(settings={}, errors=[])


_mdm_cache: MdmResult | None = None
_hkcu_cache: MdmResult | None = None
# Marks whether the (async) startup load has been kicked off.
_mdm_loaded = False


# ---------------------------------------------------------------------------
# Startup load — fires early, awaited before first settings read
# ---------------------------------------------------------------------------


async def start_mdm_settings_load() -> None:
    """Kick off the async MDM/HKCU reads and populate the caches.

    Idempotent. Unlike the TS (which stores an in-flight promise), this awaits the read inline; call
    it once early in startup so the subprocess runs while modules load. Subsequent calls are no-ops.
    """
    global _mdm_loaded, _mdm_cache, _hkcu_cache
    if _mdm_loaded:
        return
    _mdm_loaded = True

    profile_checkpoint("mdm_load_start")
    import time

    start_time = time.time() * 1000

    # Use the startup raw read if cli fired it, otherwise fire a fresh one. Both paths produce the
    # same RawReadResult; _consume_raw_read_result parses it.
    raw_promise = get_mdm_raw_read_promise()
    raw = await raw_promise if raw_promise is not None else await fire_raw_read()

    consumed = _consume_raw_read_result(raw)
    _mdm_cache = consumed["mdm"]
    _hkcu_cache = consumed["hkcu"]
    profile_checkpoint("mdm_load_end")

    duration = time.time() * 1000 - start_time
    log_for_debugging(f"MDM settings load completed in {duration}ms")
    if len(_mdm_cache.settings) > 0:
        log_for_debugging(f"MDM settings found: {', '.join(_mdm_cache.settings.keys())}")
        try:
            log_for_diagnostics_no_pii(
                "info",
                "mdm_settings_loaded",
                {
                    "duration_ms": duration,
                    "key_count": len(_mdm_cache.settings),
                    "error_count": len(_mdm_cache.errors),
                },
            )
        except Exception:  # noqa: BLE001 — diagnostic logging is best-effort (TS empty catch).
            pass


async def ensure_mdm_settings_loaded() -> None:
    """Await the in-flight MDM load.

    Triggers :func:`start_mdm_settings_load` if it has not run yet. Call before the first settings
    read; resolves immediately once loaded.
    """
    if not _mdm_loaded:
        await start_mdm_settings_load()


# ---------------------------------------------------------------------------
# Sync cache readers — used by the settings pipeline (load_settings_from_disk)
# ---------------------------------------------------------------------------


def get_mdm_settings() -> MdmResult:
    """Admin-controlled MDM settings from the session cache.

    Returns settings from admin-only sources (macOS ``/Library/Managed Preferences/`` requiring
    root; Windows HKLM requiring admin). Does NOT include HKCU — use :func:`get_hkcu_settings`.
    """
    return _mdm_cache if _mdm_cache is not None else _empty_result()


def get_hkcu_settings() -> MdmResult:
    """Return user-writable HKCU registry settings at the lowest policy priority.

    Only relevant on Windows — empty on other platforms.
    """
    return _hkcu_cache if _hkcu_cache is not None else _empty_result()


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def clear_mdm_settings_cache() -> None:
    """Clear the MDM + HKCU caches, forcing a fresh read on next load (``clearMdmSettingsCache``)."""
    global _mdm_cache, _hkcu_cache, _mdm_loaded
    _mdm_cache = None
    _hkcu_cache = None
    _mdm_loaded = False


def set_mdm_settings_cache(mdm: MdmResult, hkcu: MdmResult) -> None:
    """Update the session caches directly."""
    global _mdm_cache, _hkcu_cache
    _mdm_cache = mdm
    _hkcu_cache = hkcu


# ---------------------------------------------------------------------------
# Refresh — fires a fresh raw read, parses, returns results.
# ---------------------------------------------------------------------------


async def refresh_mdm_settings() -> dict[str, MdmResult]:
    """Fire a fresh MDM subprocess read and parse it.

    Returns ``{"mdm": MdmResult, "hkcu": MdmResult}``. Does NOT update the cache — the caller
    decides whether to apply (used by the 30-minute poll in ``changeDetector``).
    """
    raw = await fire_raw_read()
    return _consume_raw_read_result(raw)


# ---------------------------------------------------------------------------
# Parsing — converts raw subprocess output to validated MdmResult
# ---------------------------------------------------------------------------


def parse_command_output_as_settings(stdout: str, source_path: str) -> MdmResult:
    """Parse plutil/registry JSON output into validated settings (``parseCommandOutputAsSettings``).

    Filters invalid permission rules before schema validation so one bad rule doesn't reject the
    entire MDM settings blob.
    """
    data = safe_parse_json(stdout, False)
    if not data or not isinstance(data, dict):
        return MdmResult(settings={}, errors=[])

    rule_warnings = filter_invalid_permission_rules(data, source_path)
    try:
        parsed = SettingsJson.model_validate(data)
    except Exception as err:  # noqa: BLE001 — mirror zod safeParse: any validation failure.
        from pydantic import ValidationError as PydanticValidationError

        if isinstance(err, PydanticValidationError):
            errors = format_zod_error(err, source_path)
        else:  # Non-validation error — surface as a single opaque issue.
            errors = [ValidationError(file=source_path, path="", message=str(err))]
        return MdmResult(settings={}, errors=[*rule_warnings, *errors])

    return MdmResult(
        settings=parsed.model_dump(by_alias=True, exclude_none=True),
        errors=rule_warnings,
    )


def parse_reg_query_stdout(stdout: str, value_name: str = "Settings") -> str | None:
    """Extract a registry string value from ``reg query`` stdout.

    Matches both ``REG_SZ`` and ``REG_EXPAND_SZ``, case-insensitive. Expected line shape::

        Settings    REG_SZ    {"json":"value"}
    """
    lines = re.split(r"\r?\n", stdout)
    escaped = re.sub(r"[.*+?^${}()|[\]\\]", r"\\\g<0>", value_name)
    pattern = re.compile(rf"^\s+{escaped}\s+REG_(?:EXPAND_)?SZ\s+(.*)$", re.IGNORECASE)
    for line in lines:
        match = pattern.match(line)
        if match and match.group(1):
            return match.group(1).rstrip()
    return None


def _consume_raw_read_result(raw: RawReadResult) -> dict[str, MdmResult]:
    """Convert raw subprocess output into MDM/HKCU results.

    Applies the first-source-wins policy. Returns ``{"mdm": MdmResult, "hkcu": MdmResult}``.
    """
    # macOS: plist result (first source wins — already filtered in raw_read).
    if raw.plist_stdouts:
        first = raw.plist_stdouts[0]
        result = parse_command_output_as_settings(first["stdout"], first["label"])
        if len(result.settings) > 0:
            return {"mdm": result, "hkcu": _empty_result()}

    # Windows: HKLM result.
    if raw.hklm_stdout:
        json_string = parse_reg_query_stdout(raw.hklm_stdout)
        if json_string:
            result = parse_command_output_as_settings(
                json_string,
                f"Registry: {WINDOWS_REGISTRY_KEY_PATH_HKLM}\\{WINDOWS_REGISTRY_VALUE_NAME}",
            )
            if len(result.settings) > 0:
                return {"mdm": result, "hkcu": _empty_result()}

    # No admin MDM — check managed-settings.json before using HKCU.
    if _has_managed_settings_file():
        return {"mdm": _empty_result(), "hkcu": _empty_result()}

    # Fall through to HKCU (already read in parallel).
    if raw.hkcu_stdout:
        json_string = parse_reg_query_stdout(raw.hkcu_stdout)
        if json_string:
            result = parse_command_output_as_settings(
                json_string,
                f"Registry: {WINDOWS_REGISTRY_KEY_PATH_HKCU}\\{WINDOWS_REGISTRY_VALUE_NAME}",
            )
            return {"mdm": _empty_result(), "hkcu": result}

    return {"mdm": _empty_result(), "hkcu": _empty_result()}


def _has_managed_settings_file() -> bool:
    """True if a non-empty managed-settings.json / drop-in file exists.

    Cheap sync check used to skip HKCU when a higher-priority file-based source exists.
    """
    try:
        file_path = os.path.join(get_managed_file_path(), "managed-settings.json")
        content = read_file_sync(file_path)
        data = safe_parse_json(content, False)
        if data and isinstance(data, dict) and len(data) > 0:
            return True
    except Exception:  # noqa: BLE001 — fall through to drop-in check (TS empty catch).
        pass

    try:
        drop_in_dir = get_managed_settings_drop_in_dir()
        entries = get_fs_implementation().readdir_sync(drop_in_dir)
        for d in entries:
            if (
                not (d.is_file() or d.is_symbolic_link())
                or not d.name.endswith(".json")
                or d.name.startswith(".")
            ):
                continue
            try:
                content = read_file_sync(os.path.join(drop_in_dir, d.name))
                data = safe_parse_json(content, False)
                if data and isinstance(data, dict) and len(data) > 0:
                    return True
            except Exception:  # noqa: BLE001 — skip unreadable/malformed file (TS empty catch).
                pass
    except Exception:  # noqa: BLE001 — drop-in dir doesn't exist (TS empty catch).
        pass

    return False
