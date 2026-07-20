"""Browser driver inventory + on-demand install — powers the console's driver picker.

Each engine in ``BROWSER_ENGINE_CATALOG`` falls into one of four buckets:
  - ``playwright`` (chromium / firefox / webkit)  — a browser Playwright can DOWNLOAD on demand.
  - ``system``     (chrome / edge / brave / …)     — your own installed app; auto-detected, not downloaded.
  - ``stealth``    (cloak / camoufox)              — needs a Python extra; downloads its binary on first launch.
  - ``remote``     (cdp / connect / browserless …) — attaches to a browser you run; nothing to download.

``list_drivers()`` reports every driver with its install state + next step; ``install_browser()``
runs ``playwright install <browser>`` for the downloadable kernels.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncGenerator
from typing import Any

from tabvis.utils.debug import log_for_debugging

# The Playwright kernels that can be downloaded on demand (engine key == playwright browser name).
INSTALLABLE = ("chromium", "firefox", "webkit")


def _installed_playwright() -> dict[str, bool]:
    """Which Playwright browsers are actually on disk (executable path exists). Runs the driver, so
    call it off the event loop via ``asyncio.to_thread``."""
    out: dict[str, bool] = {}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            for b in INSTALLABLE:
                try:
                    out[b] = os.path.exists(getattr(p, b).executable_path)
                except Exception:  # noqa: BLE001 — not installed / path unavailable
                    out[b] = False
    except Exception as e:  # noqa: BLE001 — playwright missing or driver failed to start
        log_for_debugging(f"[DRIVERS] playwright status check failed: {e}")
    return out


def _category(spec: Any) -> str:
    if spec.key in INSTALLABLE:
        return "playwright"
    if spec.mode in ("cdp", "connect"):
        return "remote"
    if spec.requires:  # plugin / stealth engines carry a required package
        return "stealth"
    return "system"


def _hint(spec: Any, category: str, installed: bool | None) -> str:
    if category == "playwright":
        return "Installed." if installed else f"Download the Playwright {spec.kernel} browser (~150 MB)."
    if category == "stealth":
        extra = "cloak" if spec.requires == "cloakbrowser" else (spec.requires or "")
        return (
            "Installed — downloads its patched binary on first launch."
            if installed
            else f"Needs the {spec.requires} package: `uv sync --extra {extra}`."
        )
    if category == "remote":
        return spec.notes or "Attach to a browser you run — set its endpoint."
    return spec.notes or "Uses your installed browser (auto-detected)."


async def list_drivers() -> dict[str, Any]:
    """The full driver catalog with per-driver install state and next step."""
    from tabvis.utils.browser_config import (
        BROWSER_ENGINE_CATALOG,
        engine_package_available,
        playwright_available,
    )

    pw = playwright_available()
    installed_pw = await asyncio.to_thread(_installed_playwright) if pw else {}

    drivers: list[dict[str, Any]] = []
    for key, spec in BROWSER_ENGINE_CATALOG.items():
        category = _category(spec)
        if category == "playwright":
            installed: bool | None = bool(installed_pw.get(spec.browser_type))
        elif category == "stealth":
            installed = engine_package_available(spec.requires)
        else:
            installed = None  # system app / remote endpoint — not something we can detect here
        drivers.append(
            {
                "key": key,
                "label": spec.label,
                "kernel": spec.kernel,
                "browser_type": spec.browser_type,
                "mode": spec.mode,
                "stealth": bool(spec.stealth),
                "requires": spec.requires,
                "category": category,
                "installable": category == "playwright",
                "installed": installed,
                "hint": _hint(spec, category, installed),
            }
        )
    return {"playwright_installed": pw, "drivers": drivers}


async def install_browser_stream(browser: str) -> AsyncGenerator[dict[str, Any], None]:
    """Run ``playwright install <browser>`` and stream progress, then a final result.

    Yields ``{"type": "progress", "text": …}`` lines as the download proceeds (the playwright CLI's
    output, split on CR/LF so the in-place progress bar surfaces as updates), then exactly one
    ``{"type": "result", …}``. An unknown browser yields a single ``{"type": "error", …}``.
    """
    browser = (browser or "").strip().lower()
    if browser not in INSTALLABLE:
        yield {
            "type": "error",
            "error": f"'{browser}' is not a downloadable Playwright browser "
            f"(choose one of {', '.join(INSTALLABLE)}).",
        }
        return

    log_for_debugging(f"[DRIVERS] streaming install of playwright {browser} …")
    yield {"type": "progress", "text": f"Starting download of {browser}…"}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            browser,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001
        yield {"type": "result", "ok": False, "browser": browser, "installed": False, "message": f"could not start install: {e}"}
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
        buf = segments.pop()  # keep the incomplete tail for the next chunk
        for seg in segments:
            seg = seg.strip()
            if seg and seg != last:  # dedupe consecutive identical progress-bar frames
                last = seg
                yield {"type": "progress", "text": seg}
    if buf.strip() and buf.strip() != last:
        yield {"type": "progress", "text": buf.strip()}

    code = await proc.wait()
    installed = (await asyncio.to_thread(_installed_playwright)).get(browser, False)
    ok = code == 0 and installed
    yield {
        "type": "result",
        "ok": ok,
        "browser": browser,
        "installed": installed,
        "message": f"{browser} is installed" if ok else f"install failed (exit {code})",
    }


async def install_browser(browser: str) -> dict[str, Any]:
    """Download a Playwright browser via ``playwright install <browser>`` (chromium/firefox/webkit)."""
    browser = (browser or "").strip().lower()
    if browser not in INSTALLABLE:
        return {
            "ok": False,
            "error": f"'{browser}' is not a downloadable Playwright browser (choose one of "
            f"{', '.join(INSTALLABLE)}). System browsers are your own apps; stealth engines use "
            f"`uv sync --extra …`; remote engines attach to an endpoint.",
        }

    from tabvis.utils.exec_file_no_throw import exec_file_no_throw

    log_for_debugging(f"[DRIVERS] installing playwright {browser} …")
    res = await exec_file_no_throw(
        sys.executable, ["-m", "playwright", "install", browser], {"timeout": 600_000, "use_cwd": False}
    )
    code = res.get("code")
    output = ((res.get("stdout") or "") + (res.get("stderr") or "")).strip()
    installed = (await asyncio.to_thread(_installed_playwright)).get(browser, False)
    ok = code == 0 and installed
    return {
        "ok": ok,
        "browser": browser,
        "installed": installed,
        "message": f"{browser} is installed" if ok else f"install failed (exit {code})",
        "output": output[-2000:],
    }
