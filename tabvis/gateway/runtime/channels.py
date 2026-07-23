"""ChannelRuntime — mount the IM channel plugins into the gateway (design §4, Phase 4).

This is the piece that makes the channel plugins *live*: it registers the configured plugins + their
accounts onto a :class:`ChannelGateway`, drives inbound HTTP webhooks through the gateway's pipeline
(verify → normalize → bind → Run), starts the client-loop channels' read loops, and — the other half —
delivers each finished Run's result back to the channel it came from.

Outbound is event-driven: it subscribes to the durable log's live bus and, on ``run.completed`` /
``run.failed`` for a Run whose conversation is bound to a channel, sends the Run's result text back.
The result text is the ``result_preview`` the runner records on the completion event (bounded to
~2000 chars, design §7.9), which is the final assistant message — enough for a chat reply.

Configuration is env-driven: ``TABVIS_CHANNELS`` is a comma list of plugin ids to enable (e.g.
``feishu,slack,telegram``); each plugin reads its own ``TABVIS_<PLATFORM>_*`` vars via ``from_env``. A
plugin that fails to configure is recorded as an error and skipped — one misconfigured channel never
breaks the others or the gateway.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, Mapping

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins import load_channel_plugin_class
from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.events.subscriptions import LiveBus, get_live_bus
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime.run_store import RunStore, get_run_store
from tabvis.gateway.store import db
from tabvis.utils.debug import log_for_debugging


class ChannelRuntime:
    def __init__(
        self,
        *,
        run_store: RunStore | None = None,
        events: EventStore | None = None,
        live_bus: LiveBus | None = None,
    ) -> None:
        self.gateway = ChannelGateway()
        self._runs = run_store or get_run_store()
        self._events = events or get_event_store()
        self._bus = live_bus or get_live_bus()
        self._plugins: dict[str, Any] = {}     # plugin_id -> plugin instance
        self._accounts: dict[str, str] = {}    # plugin_id -> channel_account_id
        self._errors: dict[str, str] = {}      # plugin_id -> config/start error
        self._queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._unsubscribe = None

    # --- registration ---------------------------------------------------------------------------

    def register(self, plugin_id: str, plugin: Any, *, account_id: str | None = None) -> None:
        account_id = account_id or f"ca_{plugin_id}"
        self.gateway.register_plugin(plugin)
        self.gateway.register_account(
            ChannelAccount(channel_account_id=account_id, plugin_id=plugin.manifest.plugin_id)
        )
        self._plugins[plugin_id] = plugin
        self._accounts[plugin_id] = account_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "ChannelRuntime | None":
        """Build a runtime from ``TABVIS_CHANNELS``; ``None`` when no channels are enabled."""
        source = env if env is not None else os.environ
        enabled = [p.strip() for p in (source.get("TABVIS_CHANNELS") or "").split(",") if p.strip()]
        if not enabled:
            return None
        runtime = cls(**kwargs)
        for plugin_id in enabled:
            try:
                plugin_cls = load_channel_plugin_class(plugin_id)
            except KeyError:
                runtime._errors[plugin_id] = "unknown channel plugin"
                continue
            try:
                plugin = plugin_cls.from_env(env) if env is not None else plugin_cls.from_env()
                runtime.register(plugin_id, plugin)
            except Exception as exc:  # noqa: BLE001 - one misconfigured channel must not break the rest
                runtime._errors[plugin_id] = f"{type(exc).__name__}: {exc}"
                log_for_debugging(f"[CHANNELS] {plugin_id} not configured: {exc}")
        return runtime

    # --- lifecycle ------------------------------------------------------------------------------

    async def start(self) -> None:
        self._unsubscribe = self._bus.subscribe(self._on_event)
        self._worker = asyncio.ensure_future(self._deliver_loop())
        for plugin_id, plugin in list(self._plugins.items()):
            try:
                await self.gateway.start_plugin(plugin.manifest.plugin_id)
            except Exception as exc:  # noqa: BLE001
                self._errors[plugin_id] = f"start failed: {exc}"
                log_for_debugging(f"[CHANNELS] {plugin_id} failed to start: {exc}")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker = None
        for plugin in list(self._plugins.values()):
            try:
                await self.gateway.registry.stop(plugin.manifest.plugin_id)
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass

    # --- inbound (transport-facing) -------------------------------------------------------------

    async def ingest_webhook(
        self,
        plugin_id: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        params: Mapping[str, str] | None = None,
    ) -> dict:
        """Verify + ingest a webhook. Returns ``{challenge}`` for a handshake, else ``{ok, results}``.

        Raises ``GatewayError`` for an unknown/non-webhook channel or a rejected (bad signature/token)
        request.
        """
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise GatewayError("NOT_FOUND", message=f"channel {plugin_id!r} is not configured")
        handle = getattr(plugin, "handle_webhook", None)
        if handle is None:
            raise GatewayError("VALIDATION_FAILED", message=f"channel {plugin_id!r} has no webhook endpoint")

        # Some channels (WhatsApp) also decode a GET handshake via query params; pass them when accepted.
        if "params" in inspect.signature(handle).parameters:
            result = handle(dict(headers), raw_body, params=dict(params or {}))
        else:
            result = handle(dict(headers), raw_body)

        if getattr(result, "rejected", False):
            raise GatewayError("FORBIDDEN", message=getattr(result, "reason", None) or "webhook rejected")
        validation = getattr(result, "validation", None)
        if validation is not None:  # a custom handshake response body (e.g. QQ's op-13), returned verbatim
            return {"body": validation}
        challenge = getattr(result, "challenge", None)
        if challenge is not None:
            return {"challenge": challenge}
        raw = getattr(result, "raw", None)
        if raw is None:
            return {"ok": True, "results": []}
        results = await self.gateway.receive_webhook(self._accounts[plugin_id], raw)
        return {"ok": True, "results": [r.to_dict() for r in results]}

    def has_channel(self, plugin_id: str) -> bool:
        return plugin_id in self._plugins

    # --- outbound (run.completed -> channel) ----------------------------------------------------

    def _on_event(self, envelope: Any) -> None:
        # Synchronous bus listener: enqueue the completion; the async worker does the delivery.
        if envelope.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
            text = (envelope.data or {}).get("result_preview", "")
            self._queue.put_nowait((envelope.aggregate_id, envelope.type, text))

    async def _deliver_loop(self) -> None:
        while True:
            run_id, event_type, text = await self._queue.get()
            try:
                await self._deliver(run_id, event_type, text)
            except Exception as exc:  # noqa: BLE001 - an outbound failure is logged, never fatal
                log_for_debugging(f"[CHANNELS] outbound for run {run_id} failed: {exc}")

    async def _deliver(self, run_id: str, event_type: str, text: str) -> None:
        run = self._runs.get_run(run_id)
        if run is None or not run.conversation_id:
            return
        binding = db.get_binding_by_conversation(run.conversation_id)
        if not binding:
            return  # a run that did not originate from a channel — nothing to deliver
        account_id = binding.get("channel_account_id")
        if not account_id:
            return
        body = text if event_type == EventType.RUN_COMPLETED else (text or "⚠️ the run failed.")
        if not body:
            return
        await self.gateway.deliver(
            account_id,
            OutboundMessage(
                delivery_id=f"dlv_{run_id}",  # one delivery per run — the delivery layer is idempotent on this
                conversation_id=run.conversation_id,
                run_id=run_id,
                text=body,
                final=True,
            ),
        )

    # --- readiness ------------------------------------------------------------------------------

    def health(self) -> dict:
        status: dict[str, str] = {}
        for plugin_id in self._plugins:
            status[plugin_id] = "error: " + self._errors[plugin_id] if plugin_id in self._errors else "ready"
        for plugin_id, error in self._errors.items():
            status.setdefault(plugin_id, "error: " + error)
        return status
