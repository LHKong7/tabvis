"""Tabvis Agent Gateway (``docs/AGENT_GATEWAY_DESIGN.md``).

The gateway is the stable control plane in front of the runtime: one ingress for Web/CLI/HTTP and
future messaging channels, a durable append-only event log, and an explicit split between a durable
**Agent** and an immutable **Run** (one prompt-to-terminal execution).

This package is built **incrementally and additively**. Nothing here changes the existing
``tabvis/browser/server.py`` control plane yet; these modules are the seams the design's later phases
(gateway extraction, channels, interactions) slot into. The build order mirrors the design's
preferred order:

    IDs and schemas → RunRecord → durable EventStore/outbox → cursor subscriptions → …

See the design's §14 (reference layout), §15 (implementation plan), and §19 (execution rules).
"""

from __future__ import annotations

PROTOCOL = "tabvis.gateway.v1"
"""The wire protocol identifier stamped on every command and event envelope (design §9.1)."""
