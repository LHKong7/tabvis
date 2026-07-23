"""Signal channel plugin (client-loop transport: signal-cli JSON-RPC socket).

Not derived from Hermes (which has no Signal adapter) — built against the standard ``signal-cli``
JSON-RPC daemon, the usual programmatic path for a Signal bot.
"""

from __future__ import annotations

from tabvis.channels.plugins.signal.channel import PLUGIN_ID, SignalChannel
from tabvis.channels.plugins.signal.client import SignalConfig, SignalConnection

__all__ = ["SignalChannel", "SignalConfig", "SignalConnection", "PLUGIN_ID"]
