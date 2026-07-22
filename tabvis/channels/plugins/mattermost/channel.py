"""MattermostChannel — a Mattermost channel plugin on the client-loop transport (design §4.2, §4.8).

Unlike Feishu (an HTTP webhook), Mattermost's live inbound is a **persistent WebSocket**: connect to
``{url}/api/v4/websocket``, send an in-band ``authentication_challenge`` frame (auth is a message, not
an HTTP header), then stream ``posted`` events. This makes it a :class:`ClientLoopChannel` — a
background read loop pushes each message straight into the inbound pipeline via ``services.submit_inbound``
(the same door the Web console uses), and there is no ``normalize``/``handle_webhook`` path.

``httpx`` has no WebSocket client and we may not add a dependency, so the read source is *injectable*:

* ``source=None`` (production) builds the real transport in :meth:`_run_loop`, which lazily imports the
  optional ``websockets`` extra and raises a clear hint if it is absent — the dep is never added and
  tests never touch this path.
* ``source=<async iterable / async callable>`` (tests, out-of-process bridges) feeds canned raw events
  straight into the loop, so :meth:`_to_inbound` — the platform-specific parse — is unit-testable.

Outbound text is a plain ``POST /api/v4/posts`` (see :mod:`.client`), addressed to the Mattermost
channel the run's conversation is bound to.

Wiring sketch::

    mm = MattermostChannel.from_env()               # reads TABVIS_MATTERMOST_*
    gateway.register_plugin(mm)
    gateway.register_account(ChannelAccount(channel_account_id="ca_mattermost", plugin_id="mattermost"))
    await gateway.start_plugin("mattermost")         # launches the WS read loop as a background task
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Mapping

from tabvis.channels.core.contract import (
    CAP_TEXT_INBOUND,
    CAP_TEXT_OUTBOUND,
    ChannelManifest,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
)
from tabvis.channels.plugins._platform.loop import ClientLoopChannel
from tabvis.channels.plugins.mattermost.client import MattermostClient, MattermostConfig

PLUGIN_ID = "mattermost"

# Mattermost tags the channel kind on the event's `data` (NOT inside the post): D=dm, G/P=group, O=public.
_DM = "D"


class MattermostChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        # Plain text in/out only. No stream.incremental: outbound is a fresh POST per message; Mattermost
        # post-edit streaming (PUT /posts/{id}/patch) is deliberately not implemented here.
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # client-loop channels receive no webhooks; the framework HMAC gate is moot
    )

    def __init__(
        self,
        config: MattermostConfig,
        *,
        client: MattermostClient | None = None,
        source: Any = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else MattermostClient(config)
        # The raw-event read source. None → build the real WS transport lazily in _run_loop.
        self._source = source
        # A client-loop plugin serves a single account; take it from config (default "ca_<pkg>").
        self._account_id = config.channel_account_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MattermostChannel":
        return cls(MattermostConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()          # cancels the read loop, drops services
        await self._client.aclose()   # then release the REST client's HTTP resources

    # --- read loop -------------------------------------------------------------------------------

    async def _run_loop(self) -> None:
        source = self._source if self._source is not None else self._connect_live()
        # Accept an async-iterable of raw events, or a (possibly async) callable that returns one.
        if not hasattr(source, "__aiter__") and callable(source):
            source = source()
        if inspect.isawaitable(source):
            source = await source
        async for raw in source:
            message = self._to_inbound(raw)
            if message is not None:
                await self._submit(self._account_id, message)

    def _connect_live(self):
        """Build the real Mattermost WebSocket source, or fail with a clear hint.

        ``httpx`` has no WS client and the base deps are stdlib + httpx + cryptography, so the live
        transport rides an *optional* ``websockets`` extra. We import it lazily: absent → a RuntimeError
        naming the fix; present → the streaming generator below. Tests never reach here (they inject
        ``source``), so this path is exercised only with the extra installed.
        """
        try:
            import websockets  # noqa: F401 - optional; declared under the `mattermost` extra, not a base dep
        except ImportError as exc:  # pragma: no cover - depends on an optional extra being absent
            raise RuntimeError(
                "Mattermost live inbound needs a websocket client: run `uv sync --extra mattermost`. "
                "For tests or an out-of-process bridge, pass source=<async iterator of raw events> instead."
            ) from exc
        return self._ws_events()

    async def _ws_events(self):  # pragma: no cover - live path, only with the websockets extra
        import websockets

        # Resolve our own identity once so self-echo + mention stripping work even if unconfigured.
        if not self._config.bot_user_id or not self._config.bot_username:
            try:
                self._config.bot_user_id, self._config.bot_username = await self._client.get_me()
            except Exception:  # noqa: BLE001 - identity is best-effort; the loop can still run
                pass
        async with websockets.connect(self._config.websocket_url, ping_interval=30) as ws:
            # Auth is in-band: the first frame is an authentication_challenge, not an HTTP header.
            await ws.send(
                json.dumps(
                    {"seq": 1, "action": "authentication_challenge", "data": {"token": self._config.token}}
                )
            )
            async for frame in ws:
                try:
                    yield json.loads(frame)
                except (ValueError, TypeError):
                    continue  # non-JSON / control frames are ignored; the loop keeps reading

    # --- inbound parse (the unit-testable core) --------------------------------------------------

    def _to_inbound(self, raw: Any) -> InboundMessage | None:
        """Parse one raw Mattermost WS event into a normalized message, or ``None`` to skip it.

        Skips everything that must not become a prompt: non-``posted`` events (``typing``/``status``/…),
        edits (which arrive as ``post_edited``, not ``posted``), system posts, the bot's own echoes, and
        empty/attachment-only posts. The mention token is stripped so the agent sees clean input.
        """
        if not isinstance(raw, dict) or raw.get("event") != "posted":
            return None  # only `posted` carries a new message; edits come as `post_edited`
        data = raw.get("data") or {}
        # The post is double-encoded: a JSON *string* nested at data.post → decode a second time.
        post = _load_post(data.get("post"))
        if not post:
            return None
        # System posts (join/leave/header change) carry a truthy `type`; never react to them.
        if post.get("type"):
            return None
        user_id = str(post.get("user_id") or "")
        # Drop the bot's own messages so a reply never loops back in as a fresh prompt.
        if self._config.bot_user_id and user_id == self._config.bot_user_id:
            return None
        channel_id = str(post.get("channel_id") or "")
        if not channel_id:
            return None
        text = _strip_mention(
            str(post.get("message") or ""),
            self._config.bot_username,
            self._config.bot_user_id,
        )
        if not text:
            return None  # empty or attachment-only post — nothing to run
        return InboundMessage(
            # post.id is the platform's per-message id → the gateway's dedupe key.
            external_event_id=str(post.get("id") or ""),
            external_conversation_id=channel_id,
            external_account_ref=self._account_id,
            text=text,
            external_user_id=user_id or None,
        )

    # --- outbound --------------------------------------------------------------------------------

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        channel_id = self._resolve_channel_id(outbound)
        if not channel_id:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external channel id for conversation"
            )
        try:
            message_id = await self._client.send_text(channel_id, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(
            outbound.delivery_id, status="succeeded", external_message_id=message_id
        )

    def _resolve_channel_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the Mattermost channel_id is the binding's
        # external id.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- helpers -----------------------------------------------------------------------------------


def _load_post(raw: Any) -> dict:
    """Decode ``data.post`` — a JSON *string* on the wire, though tests may pass a dict directly."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001 - a malformed post string is dropped, not raised
            return {}
    return {}


def _strip_mention(text: str, username: str, user_id: str) -> str:
    """Remove a leading/inline ``@bot`` mention (by username or id, case-insensitive) and trim.

    Mattermost delivers channel messages with the ``@bot`` token intact (e.g. ``"@hermes-bot 2+2"``);
    stripping it gives the agent clean input (``"2+2"``). DMs simply carry no mention to strip.
    """
    if not text:
        return ""
    tokens = [t for t in (username, user_id) if t]
    if tokens:
        pattern = re.compile("@(?:" + "|".join(re.escape(t) for t in tokens) + ")", re.IGNORECASE)
        text = pattern.sub("", text)
    return text.strip()
