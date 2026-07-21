"""Browser Runtime — durable identity, leased bindings, observable workspaces (design §10).

The Browser Runtime owns browser *identity*, *leases*, *tabs/DOM/network/downloads*, and *policy* — but
not what the agent should click (design §10.1). Agent tools receive a ``binding_id``, never a raw
Playwright object (design §10.4), and the exclusive claim on a profile is an atomic, lease-backed,
durable binding so two isolated agents run in parallel while a shared profile has exactly one active
writer (design §10.5).

Real DOM driving lives behind an injected :class:`BrowserDriver` seam, so the ownership/lease/recovery
guarantees this package enforces are deterministically testable without launching a browser. The
concrete :class:`ManagerBrowserDriver` bridges that seam to tabvis's real ``browser`` subsystem;
:func:`real_browser_runtime` composes the two for a daemon.
"""

from __future__ import annotations


def real_browser_runtime(*, model: str = "", cwd: str | None = None):
    """A :class:`BrowserRuntime` wired to drive real Chromium via the tabvis manager (design §10)."""
    from tabvis.gateway.runtime.browser.manager_driver import ManagerBrowserDriver
    from tabvis.gateway.runtime.browser.runtime import BrowserRuntime

    return BrowserRuntime(driver=ManagerBrowserDriver(model=model, cwd=cwd))
