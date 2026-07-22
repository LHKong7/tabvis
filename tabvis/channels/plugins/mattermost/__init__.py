"""Mattermost channel plugin (design §4.8).

A client-loop channel for Mattermost: holds a persistent WebSocket to ``/api/v4/websocket``, parses
``posted`` events into inbound messages, and sends replies through the ``/api/v4/posts`` REST API. See
:mod:`tabvis.channels.plugins.mattermost.channel` for the transport + wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.mattermost.channel import PLUGIN_ID, MattermostChannel
from tabvis.channels.plugins.mattermost.client import MattermostClient, MattermostConfig

__all__ = ["MattermostChannel", "MattermostConfig", "MattermostClient", "PLUGIN_ID"]
