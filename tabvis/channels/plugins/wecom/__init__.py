"""企业微信 / WeCom self-built-app callback channel plugin (design §4.8).

A webhook channel for WeCom's callback mode: verifies the SHA1 ``msg_signature`` and decrypts the
AES-256-CBC ``WXBizMsgCrypt`` envelope, normalizes the inner message XML into an inbound message, and
sends replies through the WeCom ``message/send`` API. See
:mod:`tabvis.channels.plugins.wecom.channel` for the wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.wecom.channel import (
    PLUGIN_ID,
    WeComChannel,
    WeComWebhookResult,
)
from tabvis.channels.plugins.wecom.client import WeComClient, WeComConfig

__all__ = ["WeComChannel", "WeComConfig", "WeComClient", "WeComWebhookResult", "PLUGIN_ID"]
