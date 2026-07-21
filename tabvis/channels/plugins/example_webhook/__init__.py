"""Proof webhook channel (design §4.8).

The minimal external channel that exercises the parts a real messaging integration needs: signed
webhooks and retry idempotency. It is intentionally tiny — a signed JSON body in, a recorded outbound
out — so the framework's guarantees are what the tests observe, not the channel's cleverness.
"""

from __future__ import annotations

from tabvis.channels.plugins.example_webhook.channel import PLUGIN_ID, ExampleWebhookChannel

__all__ = ["ExampleWebhookChannel", "PLUGIN_ID"]
