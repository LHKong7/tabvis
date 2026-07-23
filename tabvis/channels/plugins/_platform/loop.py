"""``ClientLoopChannel`` — base for channels that hold a persistent connection, not a webhook.

A gateway/long-poll/socket platform (Telegram long-poll, Discord gateway, Matrix sync, Mattermost
websocket, IRC, SimpleX) can't be driven by :meth:`ChannelGateway.receive_webhook`. Instead
:meth:`start` launches a background task that reads the platform and pushes each message straight
into the inbound pipeline via ``services.submit_inbound`` — the same entry the Web console uses
(:meth:`WebChannel.submit_console_message`). Subclasses implement :meth:`_run_loop` (the read loop)
and :meth:`deliver` (sending); :meth:`normalize` is inert because messages never arrive as webhooks.

The read loop is guarded: a crash marks the channel ``degraded`` (surfaced by :meth:`health`) rather
than taking down the process, and :meth:`stop` cancels it cleanly.
"""

from __future__ import annotations

import asyncio

from tabvis.channels.core.contract import (
    ChannelHealth,
    ChannelManifest,
    ChannelServices,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    RawInbound,
)


class ClientLoopChannel:
    manifest: ChannelManifest

    def __init__(self) -> None:
        self._services: ChannelServices | None = None
        self._task: asyncio.Task | None = None

    async def start(self, services: ChannelServices) -> None:
        self._services = services
        self._task = asyncio.ensure_future(self._guarded_loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown swallows a loop's dying exception
                pass
        self._services = None

    async def health(self) -> ChannelHealth:
        if self._services is None:
            return ChannelHealth(status="stopped")
        if self._task is not None and self._task.done() and not self._task.cancelled():
            return ChannelHealth(status="degraded", detail="read loop exited")
        return ChannelHealth(status="ready")

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        # Client-loop channels submit inbound directly from _run_loop(); they receive no webhooks.
        return []

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    async def _guarded_loop(self) -> None:
        try:
            await self._run_loop()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a crashed read loop degrades the channel, never the process
            pass

    async def _run_loop(self) -> None:
        """The platform read loop. Subclasses call :meth:`_submit` for each received message."""
        raise NotImplementedError

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        raise NotImplementedError

    async def _submit(self, channel_account_id: str, message: InboundMessage):
        if self._services is None:
            raise RuntimeError("channel not started")
        return await self._services.submit_inbound(channel_account_id, message)
