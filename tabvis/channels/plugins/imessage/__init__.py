"""iMessage channel plugin (client-loop transport: loopback HTTP to the macOS Photon sidecar)."""

from __future__ import annotations

from tabvis.channels.plugins.imessage.channel import PLUGIN_ID, IMessageChannel
from tabvis.channels.plugins.imessage.client import IMessageConfig, IMessageSidecarClient

__all__ = ["IMessageChannel", "IMessageConfig", "IMessageSidecarClient", "PLUGIN_ID"]
