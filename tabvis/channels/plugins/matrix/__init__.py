"""Matrix IM channel plugin (client-loop transport: Client-Server /sync long-poll + room send)."""

from __future__ import annotations

from tabvis.channels.plugins.matrix.channel import PLUGIN_ID, MatrixChannel
from tabvis.channels.plugins.matrix.client import MatrixClient, MatrixConfig

__all__ = ["MatrixChannel", "MatrixConfig", "MatrixClient", "PLUGIN_ID"]
