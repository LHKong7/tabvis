"""Feishu / Lark IM channel plugin (design §4.8).

A webhook channel for Feishu's ``im.message.receive_v1`` events: verifies + decrypts the event
subscription callback, normalizes it into an inbound message, and sends replies through the Feishu
messages API. See :mod:`tabvis.channels.plugins.feishu.channel` for the wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.feishu.channel import (
    PLUGIN_ID,
    FeishuChannel,
    FeishuWebhookResult,
)
from tabvis.channels.plugins.feishu.client import FeishuClient, FeishuConfig

__all__ = ["FeishuChannel", "FeishuConfig", "FeishuClient", "FeishuWebhookResult", "PLUGIN_ID"]
