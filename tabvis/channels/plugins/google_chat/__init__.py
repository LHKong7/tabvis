"""Google Chat channel plugin (design §4.8).

A webhook channel for Google Chat ``MESSAGE`` events: verifies the Google-issued OIDC ID-token bearer
on the inbound callback, normalizes the event into an inbound message, and creates replies through the
Chat REST API. See :mod:`tabvis.channels.plugins.google_chat.channel` for the wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.google_chat.channel import (
    PLUGIN_ID,
    GoogleChatChannel,
    GoogleChatWebhookResult,
)
from tabvis.channels.plugins.google_chat.client import GoogleChatClient, GoogleChatConfig

__all__ = [
    "GoogleChatChannel",
    "GoogleChatConfig",
    "GoogleChatClient",
    "GoogleChatWebhookResult",
    "PLUGIN_ID",
]
