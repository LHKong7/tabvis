"""Browser driver inventory + on-demand install — powers the console's driver picker.

Every engine in ``BROWSER_ENGINE_CATALOG`` can be downloaded, is a system app you install yourself,
or attaches to something remote:

  install method            engines                              how ``install_browser_stream`` installs it
  ------------------------- ------------------------------------ ------------------------------------------
  Playwright download       chromium / firefox / webkit          `playwright install <engine>` (bundled)
  Playwright channel        chrome / msedge                      `playwright install <engine>` (stable channel)
  Python package (uv)       cloak / camoufox                     `uv pip install <cloakbrowser|camoufox>`
  system app (no CLI)       brave / vivaldi / opera              — install it from the vendor; auto-detected
  remote attach             cdp / connect / browserless / …      — nothing to download; set an endpoint

``list_drivers()`` reports each driver with its install state + next step; ``install_browser_stream()``
runs the install for a downloadable one and streams progress.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import re
import shutil
import sys
from collections.abc import AsyncGenerator
from typing import Any

from tabvis.utils.debug import log_for_debugging

# Playwright browsers/channels installable via `playwright install <engine key>`.
PLAYWRIGHT_INSTALL = ("chromium", "firefox", "webkit", "chrome", "msedge")
# Playwright's own bundled kernels (installed-state detectable via executable_path).
PLAYWRIGHT_BUNDLED = ("chromium", "firefox", "webkit")
# Stealth engines installed as a Python package (engine key -> pip package name).
STEALTH_PACKAGE = {"cloak": "cloakbrowser", "camoufox": "camoufox"}

# System-browser detection (best-effort, per OS): engine key -> macOS app paths / PATH command names.
_MAC_APPS = {
    "chrome": ["/Applications/Google Chrome.app"],
    "msedge": ["/Applications/Microsoft Edge.app"],
    "brave": ["/Applications/Brave Browser.app"],
    "vivaldi": ["/Applications/Vivaldi.app"],
    "opera": ["/Applications/Opera.app"],
}
_PATH_NAMES = {
    "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
    "msedge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
    "brave": ["brave-browser", "brave"],
    "vivaldi": ["vivaldi", "vivaldi-stable"],
    "opera": ["opera"],
}


def install_via(key: str) -> str | None:
    """How engine ``key`` is installed: 'playwright' | 'package' | None (not downloadable)."""
    key = (key or "").strip().lower()
    if key in PLAYWRIGHT_INSTALL:
        return "playwright"
    if key in STEALTH_PACKAGE:
        return "package"
    return None


def _installed_playwright() -> dict[str, bool]:
    """Which bundled Playwright browsers are on disk. Runs the driver — call via ``asyncio.to_thread``."""
    out: dict[str, bool] = {}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            for b in PLAYWRIGHT_BUNDLED:
                try:
                    out[b] = os.path.exists(getattr(p, b).executable_path)
                except Exception:  # noqa: BLE001
                    out[b] = False
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[DRIVERS] playwright status check failed: {e}")
    return out


def _system_browser_installed(key: str) -> bool | None:
    """Whether a system browser (chrome/edge/brave/…) is installed. None if we can't tell here."""
    if sys.platform == "darwin":
        paths = _MAC_APPS.get(key)
        if paths is None:
            return None
        return any(os.path.exists(p) for p in paths)
    names = _PATH_NAMES.get(key)
    if names is None:
        return None
    if any(shutil.which(n) for n in names):
        return True
    return None if sys.platform.startswith("win") else False  # Windows check is unreliable → unknown


def _package_installed(package: str) -> bool:
    try:
        importlib.invalidate_caches()  # a package installed after startup may not be cached yet
        return importlib.util.find_spec(package) is not None
    except Exception:  # noqa: BLE001
        return False


def _reset_availability_memo() -> None:
    """After a package install, clear browser_config's memoized availability so it re-checks (and a
    fresh launch in this same process can find the newly-installed engine)."""
    try:
        from tabvis.utils import browser_config as bc

        for attr in ("_CLOAKBROWSER_AVAILABLE", "_CAMOUFOX_AVAILABLE"):
            if hasattr(bc, attr):
                setattr(bc, attr, None)
    except Exception:  # noqa: BLE001
        pass


def _installed(spec: Any, installed_pw: dict[str, bool]) -> bool | None:
    if spec.key in PLAYWRIGHT_BUNDLED:
        return bool(installed_pw.get(spec.key))
    if spec.key in STEALTH_PACKAGE:
        return _package_installed(STEALTH_PACKAGE[spec.key])
    if spec.mode in ("cdp", "connect"):
        return None
    return _system_browser_installed(spec.key)  # chrome/msedge/brave/vivaldi/opera


def _category(spec: Any) -> str:
    if spec.key in PLAYWRIGHT_BUNDLED:
        return "playwright"
    if spec.key in STEALTH_PACKAGE:
        return "stealth"
    if spec.mode in ("cdp", "connect"):
        return "remote"
    return "system"


def _hint(spec: Any, installed: bool | None) -> str:
    key, via = spec.key, install_via(spec.key)
    if installed:
        if via == "package":
            return "Installed — downloads its patched binary on first launch."
        return "Installed."
    if via == "playwright":
        if key in PLAYWRIGHT_BUNDLED:
            return f"Download the Playwright {spec.kernel} browser (~150 MB)."
        return f"Download & install {spec.label} (the stable channel; may prompt for permission)."
    if via == "package":
        extra = "cloak" if key == "cloak" else key
        return f"Install the {STEALTH_PACKAGE[key]} package (`uv pip install {STEALTH_PACKAGE[key]}`; needs `uv`). Or `uv sync --extra {extra}`."
    if spec.mode in ("cdp", "connect"):
        return spec.notes or "Attach to a browser you run — set its endpoint."
    return spec.notes or f"Install {spec.label} from its website — tabvis auto-detects it."


async def list_drivers() -> dict[str, Any]:
    """The full driver catalog with per-driver install state and next step."""
    from tabvis.utils.browser_config import BROWSER_ENGINE_CATALOG, playwright_available

    pw = playwright_available()
    installed_pw = await asyncio.to_thread(_installed_playwright) if pw else {}

    drivers: list[dict[str, Any]] = []
    for key, spec in BROWSER_ENGINE_CATALOG.items():
        installed = _installed(spec, installed_pw)
        drivers.append(
            {
                "key": key,
                "label": spec.label,
                "kernel": spec.kernel,
                "browser_type": spec.browser_type,
                "mode": spec.mode,
                "stealth": bool(spec.stealth),
                "requires": spec.requires,
                "category": _category(spec),
                "installable": install_via(key) is not None,
                "installed": installed,
                "hint": _hint(spec, installed),
            }
        )
    return {"playwright_installed": pw, "drivers": drivers}


async def install_browser_stream(key: str) -> AsyncGenerator[dict[str, Any], None]:
    """Install a downloadable driver, streaming progress lines then a single result.

    ``playwright`` engines run ``playwright install <key>``; ``package`` engines run
    ``uv pip install <package>``. Progress is split on CR/LF so an in-place progress bar surfaces as
    updates. An engine with no install path yields one ``{"type": "error", …}``.
    """
    key = (key or "").strip().lower()
    via = install_via(key)
    if via is None:
        yield {
            "type": "error",
            "error": f"'{key}' is not downloadable via tabvis — install it yourself "
            f"(system browsers) or set an endpoint (remote engines).",
        }
        return

    if via == "playwright":
        argv = [sys.executable, "-m", "playwright", "install", key]
    else:  # package
        uv = shutil.which("uv")
        pkg = STEALTH_PACKAGE[key]
        if not uv:
            extra = "cloak" if key == "cloak" else key
            yield {
                "type": "result",
                "ok": False,
                "browser": key,
                "installed": False,
                "message": f"`uv` not found. Install {pkg} yourself: `uv sync --extra {extra}`.",
            }
            return
        argv = [uv, "pip", "install", "--python", sys.executable, pkg]

    log_for_debugging(f"[DRIVERS] installing {key} via {via}: {' '.join(argv)}")
    yield {"type": "progress", "text": f"Starting install of {key}…"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except Exception as e:  # noqa: BLE001
        yield {"type": "result", "ok": False, "browser": key, "installed": False, "message": f"could not start install: {e}"}
        return

    assert proc.stdout is not None
    buf = ""
    last = ""
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        segments = re.split(r"[\r\n]+", buf)
        buf = segments.pop()
        for seg in segments:
            seg = seg.strip()
            if seg and seg != last:
                last = seg
                yield {"type": "progress", "text": seg}
    if buf.strip() and buf.strip() != last:
        yield {"type": "progress", "text": buf.strip()}

    code = await proc.wait()
    if via == "playwright" and key in PLAYWRIGHT_BUNDLED:
        installed: bool = (await asyncio.to_thread(_installed_playwright)).get(key, False)
    elif via == "playwright":  # chrome/msedge — trust the installer's exit code
        installed = code == 0
    else:  # package
        _reset_availability_memo()
        installed = _package_installed(STEALTH_PACKAGE[key])

    ok = code == 0 and installed
    yield {
        "type": "result",
        "ok": ok,
        "browser": key,
        "installed": installed,
        "message": f"{key} is installed" if ok else f"install failed (exit {code})",
    }
