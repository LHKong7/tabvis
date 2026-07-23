"""钉钉 DingTalk IM channel plugin (design §4.8).

A webhook channel for DingTalk chatbot messages. DingTalk's live bot is Stream Mode (an outbound
WebSocket the ``dingtalk-stream`` SDK owns), which ``stdlib + httpx`` cannot speak; this plugin
instead uses DingTalk's HTTP outgoing-robot callback — verify the ``timestamp`` + ``sign`` header
pair, normalize the message, and reply through the DingTalk robot OpenAPI. See
:mod:`tabvis.channels.plugins.dingtalk.channel` for the wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.dingtalk.channel import (
    PLUGIN_ID,
    DingTalkChannel,
    DingTalkWebhookResult,
)
from tabvis.channels.plugins.dingtalk.client import DingTalkClient, DingTalkConfig

__all__ = [
    "DingTalkChannel",
    "DingTalkConfig",
    "DingTalkClient",
    "DingTalkWebhookResult",
    "PLUGIN_ID",
]
