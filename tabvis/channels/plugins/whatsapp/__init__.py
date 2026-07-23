"""WhatsApp Cloud (Meta Graph) IM channel plugin (design §4.8).

A webhook channel for WhatsApp Cloud's ``messages`` events: verifies the ``X-Hub-Signature-256`` HMAC
(and the ``hub.*`` GET subscription handshake), normalizes an inbound text message into an inbound
message, and sends replies through the Graph messages API. See
:mod:`tabvis.channels.plugins.whatsapp.channel` for the wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.whatsapp.channel import (
    PLUGIN_ID,
    WhatsAppChannel,
    WhatsAppWebhookResult,
)
from tabvis.channels.plugins.whatsapp.client import WhatsAppClient, WhatsAppConfig

__all__ = [
    "WhatsAppChannel",
    "WhatsAppConfig",
    "WhatsAppClient",
    "WhatsAppWebhookResult",
    "PLUGIN_ID",
]
