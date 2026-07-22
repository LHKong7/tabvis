"""SimpleX Chat channel plugin (design §4.8).

A client-loop channel for a local ``simplex-chat`` daemon: one persistent WebSocket is both the
inbound event stream and the outbound command channel (there is no HTTP API and no webhook). The
loop reads received text and pushes it into the inbound pipeline; replies are fire-and-forget chat
commands over the same socket. See :mod:`tabvis.channels.plugins.simplex.channel` for the wiring
sketch. The live socket needs the optional ``websockets`` package (``uv sync --extra simplex``);
it is imported lazily so the plugin stays importable and unit-testable without it.
"""

from __future__ import annotations

from tabvis.channels.plugins.simplex.channel import PLUGIN_ID, SimpleXChatChannel
from tabvis.channels.plugins.simplex.client import SimpleXClient, SimpleXConfig

__all__ = ["SimpleXChatChannel", "SimpleXConfig", "SimpleXClient", "PLUGIN_ID"]
