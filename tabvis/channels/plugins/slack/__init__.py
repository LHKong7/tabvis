"""Slack IM channel plugin (design §4.8).

A webhook channel for Slack's Events API ``message`` / ``app_mention`` events: verifies the request
signature (HMAC over Slack's ``v0:{timestamp}:{body}`` base string, with a replay window), answers the
``url_verification`` handshake, normalizes the event into an inbound message, and sends replies through
``chat.postMessage``. See :mod:`tabvis.channels.plugins.slack.channel` for the wiring sketch.
"""

from __future__ import annotations

from tabvis.channels.plugins.slack.channel import (
    PLUGIN_ID,
    SlackChannel,
    SlackWebhookResult,
)
from tabvis.channels.plugins.slack.client import SlackClient, SlackConfig

__all__ = ["SlackChannel", "SlackConfig", "SlackClient", "SlackWebhookResult", "PLUGIN_ID"]
