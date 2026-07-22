"""ChannelServices facade, plugin registry, and the ChannelGateway (design §4.1, §4.2, §4.6).

* :class:`DefaultChannelServices` is the narrow, capability-scoped object a plugin is handed on start
  (design §4.2): submit inbound, read a binding, resolve a secret. A plugin never sees a store.
* :class:`ChannelRegistry` tracks plugin lifecycle (design §4.6).
* :class:`ChannelGateway` is the composition point the transport calls: verify a webhook, normalize it
  through the owning plugin, run each message through the inbound pipeline, and drive outbound delivery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from tabvis.channels.core import signatures
from tabvis.channels.core.contract import (
    ChannelHealth,
    ChannelPlugin,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    RawInbound,
)
from tabvis.channels.core.delivery import DeliveryService
from tabvis.channels.core.identity import ChannelAccount, ConversationBinding
from tabvis.channels.core.inbound import ChannelIngress, InboundResult
from tabvis.channels.core.stores import BindingStore, ChannelAccountStore
from tabvis.gateway.protocol.errors import GatewayError

SecretResolver = Callable[[str], "str | None"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DefaultChannelServices:
    """The facade a plugin receives (design §4.2)."""

    def __init__(
        self, ingress: ChannelIngress, bindings: BindingStore, secret_resolver: SecretResolver
    ) -> None:
        self._ingress = ingress
        self._bindings = bindings
        self._resolve = secret_resolver

    async def submit_inbound(self, channel_account_id: str, message: InboundMessage) -> InboundResult:
        return await self._ingress.ingest(channel_account_id, message)

    def get_binding(self, channel_account_id: str, external_conversation_id: str) -> ConversationBinding | None:
        return self._bindings.get(channel_account_id, external_conversation_id)

    def resolve_external_conversation(self, conversation_id: str) -> str | None:
        """Map an internal conversation back to the channel's external thread id (for outbound sends)."""
        binding = self._bindings.get_by_conversation(conversation_id)
        return binding.external_conversation_id if binding is not None else None

    def resolve_secret(self, credential_ref: str) -> str | None:
        return self._resolve(credential_ref)


class ChannelRegistry:
    """In-memory plugin lifecycle (design §4.6)."""

    def __init__(self) -> None:
        self._plugins: dict[str, ChannelPlugin] = {}
        self._status: dict[str, str] = {}

    def register(self, plugin: ChannelPlugin) -> None:
        self._plugins[plugin.manifest.plugin_id] = plugin
        self._status[plugin.manifest.plugin_id] = "configured"

    def get(self, plugin_id: str) -> ChannelPlugin | None:
        return self._plugins.get(plugin_id)

    def status(self, plugin_id: str) -> str:
        return self._status.get(plugin_id, "unknown")

    async def start(self, plugin_id: str, services: DefaultChannelServices) -> None:
        plugin = self._require(plugin_id)
        self._status[plugin_id] = "starting"
        await plugin.start(services)
        self._status[plugin_id] = "ready"

    async def stop(self, plugin_id: str) -> None:
        plugin = self._require(plugin_id)
        await plugin.stop()
        self._status[plugin_id] = "stopped"

    async def health(self, plugin_id: str) -> ChannelHealth:
        return await self._require(plugin_id).health()

    def _require(self, plugin_id: str) -> ChannelPlugin:
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise GatewayError("NOT_FOUND", message=f"No channel plugin {plugin_id!r}")
        return plugin


class ChannelGateway:
    """Top-level channel composition (design §4.5 inbound + outbound flows)."""

    def __init__(self, secret_resolver: SecretResolver | None = None) -> None:
        self.accounts = ChannelAccountStore()
        self.bindings = BindingStore()
        self.ingress = ChannelIngress(binding_store=self.bindings)
        self.delivery = DeliveryService()
        self.registry = ChannelRegistry()
        self._resolve: SecretResolver = secret_resolver or (lambda ref: None)
        self.services = DefaultChannelServices(self.ingress, self.bindings, self._resolve)

    def register_account(self, account: ChannelAccount) -> ChannelAccount:
        return self.accounts.register(account)

    def register_plugin(self, plugin: ChannelPlugin) -> None:
        self.registry.register(plugin)

    async def start_plugin(self, plugin_id: str) -> None:
        await self.registry.start(plugin_id, self.services)

    async def receive_webhook(self, channel_account_id: str, raw: RawInbound) -> list[InboundResult]:
        """The §4.5 inbound flow: verify signature → normalize → ingest each message.

        A signature is required and verified *before* the body is parsed when the owning plugin marks
        its webhooks signed (design §4.5 step 1); a bad signature is rejected with ``FORBIDDEN`` and no
        message is created.
        """
        account = self.accounts.get(channel_account_id)
        if account is None:
            raise GatewayError("NOT_FOUND", message="Unknown channel account", details={"channel_account_id": channel_account_id})
        plugin = self.registry.get(account.plugin_id)
        if plugin is None:
            raise GatewayError("NOT_FOUND", message=f"No plugin {account.plugin_id!r}")

        if plugin.manifest.signed_webhooks:
            secret = self._resolve(account.credential_ref)
            if not secret or raw.raw_body is None or not signatures.verify(secret, raw.raw_body, raw.signature):
                raise GatewayError("FORBIDDEN", message="Invalid webhook signature")

        messages = await plugin.normalize(raw)
        results = [await self.ingress.ingest(channel_account_id, m) for m in messages]
        await plugin.acknowledge(raw.external_event_id)
        return results

    async def deliver(self, channel_account_id: str, outbound: OutboundMessage) -> DeliveryReceipt:
        account = self.accounts.get(channel_account_id)
        if account is None:
            raise GatewayError("NOT_FOUND", details={"channel_account_id": channel_account_id})
        plugin = self.registry.get(account.plugin_id)
        if plugin is None:
            raise GatewayError("NOT_FOUND", message=f"No plugin {account.plugin_id!r}")
        outbound.channel_account_id = outbound.channel_account_id or channel_account_id
        return await self.delivery.deliver(plugin, outbound)


class InMemorySecretResolver:
    """A trivial SecretStore stand-in for local/dev and tests (design §4.7 defers to a real store)."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def set(self, ref: str, value: str) -> None:
        self._secrets[ref] = value

    def __call__(self, ref: str) -> str | None:
        return self._secrets.get(ref)
