"""Discord channel plugin (client-loop transport: Gateway websocket in, REST send out)."""

from __future__ import annotations

from tabvis.channels.plugins.discord.channel import PLUGIN_ID, DiscordChannel
from tabvis.channels.plugins.discord.client import DiscordClient, DiscordConfig

__all__ = ["DiscordChannel", "DiscordConfig", "DiscordClient", "PLUGIN_ID"]
