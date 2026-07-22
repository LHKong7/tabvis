"""IRC channel plugin (client-loop transport: persistent TCP/TLS socket, PRIVMSG lines)."""

from __future__ import annotations

from tabvis.channels.plugins.irc.channel import PLUGIN_ID, IrcChannel
from tabvis.channels.plugins.irc.client import IrcConfig, IrcConnection

__all__ = ["IrcChannel", "IrcConfig", "IrcConnection", "PLUGIN_ID"]
