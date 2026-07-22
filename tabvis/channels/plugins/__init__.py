"""External channel plugins (design §4.8, §8.7).

Each subpackage is one IM platform implementing the
:class:`~tabvis.channels.core.contract.ChannelPlugin` contract on the shared :mod:`._platform`
foundation, following the :mod:`.feishu` reference. Two transport shapes are covered:

* **Webhook** (``feishu``, ``dingtalk``, ``slack``, ``teams``, ``line``, ``google_chat``, ``wecom``,
  ``whatsapp``) — an HTTP callback the plugin verifies and hands to the gateway.
* **Client-loop** (subclasses of :class:`._platform.loop.ClientLoopChannel`) — a persistent
  connection that pushes messages into the inbound pipeline directly.

Plugins are registered lazily by import path so importing this package pulls no platform's
dependencies; a plugin that needs an optional crypto extra only imports when it is actually loaded.
"""

from __future__ import annotations

import importlib

# plugin_id -> "module:class". The built-in IM channel plugins distilled from the Hermes reference.
BUILTIN_CHANNEL_PLUGINS: dict[str, str] = {
    # Webhook transport (verified + parsed HTTP callbacks).
    "feishu": "tabvis.channels.plugins.feishu:FeishuChannel",
    "dingtalk": "tabvis.channels.plugins.dingtalk:DingTalkChannel",
    "wecom": "tabvis.channels.plugins.wecom:WeComChannel",
    "slack": "tabvis.channels.plugins.slack:SlackChannel",
    "teams": "tabvis.channels.plugins.teams:TeamsChannel",
    "line": "tabvis.channels.plugins.line:LineChannel",
    "google_chat": "tabvis.channels.plugins.google_chat:GoogleChatChannel",
    "whatsapp": "tabvis.channels.plugins.whatsapp:WhatsAppChannel",
    # Client-loop transport (a persistent connection pushes into the inbound pipeline).
    "telegram": "tabvis.channels.plugins.telegram:TelegramChannel",
    "matrix": "tabvis.channels.plugins.matrix:MatrixChannel",
    "irc": "tabvis.channels.plugins.irc:IrcChannel",
    "discord": "tabvis.channels.plugins.discord:DiscordChannel",
    "mattermost": "tabvis.channels.plugins.mattermost:MattermostChannel",
    "simplex": "tabvis.channels.plugins.simplex:SimpleXChatChannel",
    "imessage": "tabvis.channels.plugins.imessage:IMessageChannel",
    # Reference / test channels.
    "example_webhook": "tabvis.channels.plugins.example_webhook:ExampleWebhookChannel",
}


def available_channel_plugins() -> list[str]:
    """The ids of the built-in channel plugins (keys of :data:`BUILTIN_CHANNEL_PLUGINS`)."""
    return sorted(BUILTIN_CHANNEL_PLUGINS)


def load_channel_plugin_class(plugin_id: str) -> type:
    """Lazily import and return a channel plugin class by id.

    Raises ``KeyError`` for an unknown id. The import is deferred so a plugin's optional dependency
    (e.g. ``cryptography`` for ``wecom``/``teams``/``google_chat``) is only required when that plugin
    is actually loaded.
    """
    module_name, _, attr = BUILTIN_CHANNEL_PLUGINS[plugin_id].partition(":")
    return getattr(importlib.import_module(module_name), attr)


__all__ = ["BUILTIN_CHANNEL_PLUGINS", "available_channel_plugins", "load_channel_plugin_class"]
