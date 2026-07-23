"""QQ official-bot channel plugin (webhook transport, Ed25519).

Not derived from Hermes (which has no QQ adapter) — built against Tencent's official QQ bot v2 webhook
callback + message API.
"""

from __future__ import annotations

from tabvis.channels.plugins.qq.channel import PLUGIN_ID, QQChannel, QQWebhookResult
from tabvis.channels.plugins.qq.client import QQClient, QQConfig

__all__ = ["QQChannel", "QQConfig", "QQClient", "QQWebhookResult", "PLUGIN_ID"]
