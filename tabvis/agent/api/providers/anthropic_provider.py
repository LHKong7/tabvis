"""Anthropic provider — the STRATEGY with an *identity* adapter (a passthrough).

Anthropic already speaks the canonical protocol, so this strategy needs no :class:`ProtocolAdapter`:
it builds the same ``AsyncAnthropic`` client the client module always built, and its
``create_stream`` is just ``client.beta.messages.create(stream=True)`` — the raw SDK parts are
already exactly what the model-client loop consumes. It exists so the abstraction has one uniform
seam across all three backends.
"""

from __future__ import annotations

from typing import Any

from tabvis.agent.api.client import get_provider_client


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, source: str | None = None, model: str | None = None) -> None:
        self._source = source
        self._model = model

    async def get_client(self) -> Any:
        # max_retries=0: retries are owned by with_retry in the model client, not the SDK.
        return await get_provider_client(max_retries=0, model=self._model, source=self._source)

    async def create_stream(self, client: Any, params: dict[str, Any]) -> Any:
        return await client.beta.messages.create(**params, stream=True)
