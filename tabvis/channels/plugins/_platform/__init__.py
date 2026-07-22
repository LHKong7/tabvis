"""Shared foundation for IM platform channel plugins (design §4.2, §4.8).

Every messaging platform (Feishu, DingTalk, Slack, Telegram, …) implements the same
:class:`~tabvis.channels.core.contract.ChannelPlugin` contract, but they split into two transport
shapes, and this package holds what both shapes reuse so a platform plugin only writes what is
genuinely platform-specific:

* **Webhook platforms** (Feishu, DingTalk, Teams, LINE, Slack Events, Google Chat, WeCom, WhatsApp)
  receive an HTTP callback. They verify + parse it and hand a :class:`RawInbound` to
  :meth:`ChannelGateway.receive_webhook`. Shared here: signature helpers (:mod:`webhook`) and a
  token-authenticated REST client base for sending (:mod:`rest`).
* **Client-loop platforms** (Telegram long-poll, Discord/Matrix/Mattermost/IRC/SimpleX) hold a
  persistent connection instead of a webhook. :class:`~.loop.ClientLoopChannel` runs the read loop as
  a background task and pushes each message straight into the inbound pipeline via
  ``services.submit_inbound`` — the same door the Web console uses.

Config for every platform is read from ``TABVIS_<PLATFORM>_*`` environment variables (:mod:`config`).
"""

from __future__ import annotations

from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient
from tabvis.channels.plugins._platform.webhook import (
    constant_time_eq,
    hmac_sha256_hex,
    verify_hmac_sha256,
)

__all__ = [
    "ChannelApiError",
    "RestChannelClient",
    "constant_time_eq",
    "hmac_sha256_hex",
    "verify_hmac_sha256",
]
