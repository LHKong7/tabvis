"""LINE Messaging API channel plugin (design §4.8).

A webhook channel for LINE bot events: verifies the ``X-Line-Signature`` header over the raw body,
normalizes each bundled event into an inbound message, and answers through LINE's reply-then-push
send. See :mod:`tabvis.channels.plugins.line.channel` for the wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.line.channel import (
    PLUGIN_ID,
    LineChannel,
    LineWebhookResult,
)
from tabvis.channels.plugins.line.client import LineClient, LineConfig

__all__ = ["LineChannel", "LineConfig", "LineClient", "LineWebhookResult", "PLUGIN_ID"]
