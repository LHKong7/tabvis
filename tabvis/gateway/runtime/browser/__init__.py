"""Browser Runtime — durable identity, leased bindings, observable workspaces (design §10).

The Browser Runtime owns browser *identity*, *leases*, *tabs/DOM/network/downloads*, and *policy* — but
not what the agent should click (design §10.1). Agent tools receive a ``binding_id``, never a raw
Playwright object (design §10.4), and the exclusive claim on a profile is an atomic, lease-backed,
durable binding so two isolated agents run in parallel while a shared profile has exactly one active
writer (design §10.5).

Real DOM driving lives behind an injected :class:`BrowserDriver` seam, so the ownership/lease/recovery
guarantees this package enforces are deterministically testable without launching a browser.
"""

from __future__ import annotations
