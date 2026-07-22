"""Telegram IM channel plugin (client-loop transport: Bot API long-poll + sendMessage)."""

from __future__ import annotations

from tabvis.channels.plugins.telegram.channel import PLUGIN_ID, TelegramChannel
from tabvis.channels.plugins.telegram.client import TelegramClient, TelegramConfig

__all__ = ["TelegramChannel", "TelegramConfig", "TelegramClient", "PLUGIN_ID"]
