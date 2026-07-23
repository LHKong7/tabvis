"""Microsoft Teams / Bot Framework channel plugin (design §4.8).

A webhook channel for Bot Framework message activities: validates the inbound ``Authorization: Bearer``
JWT (RS256 via the Bot Framework JWKS — no HMAC, no challenge handshake), normalizes the activity into
an inbound message, and sends replies through the Bot Framework REST API (OAuth2 client-credentials +
``/v3/conversations/{id}/activities``). See :mod:`tabvis.channels.plugins.teams.channel` for the wiring
sketch, and :mod:`tabvis.channels.plugins.teams.crypto` for the JWT verification.
"""

from __future__ import annotations

from tabvis.channels.plugins.teams.channel import (
    PLUGIN_ID,
    TeamsChannel,
    TeamsWebhookResult,
)
from tabvis.channels.plugins.teams.client import TeamsClient, TeamsConfig

__all__ = ["TeamsChannel", "TeamsConfig", "TeamsClient", "TeamsWebhookResult", "PLUGIN_ID"]
