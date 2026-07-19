"""Model-provider layer — a *protocol-conversion proxy* built from three patterns.

The query pipeline is Anthropic-native: it emits Anthropic request params + tool schemas and consumes
Anthropic-shaped streaming parts (see :mod:`._events`). To also drive OpenAI or Gemini without
forking any of that, this package sits at the single point where a request is sent and its stream is
read (``model_client``'s ``operation``) and converts protocols there. Three roles, deliberately named:

* **Strategy** — :class:`ModelProvider`. An interchangeable "how to reach this backend" object
  (build a client, open a stream). Concrete strategies: ``AnthropicProvider`` / ``OpenAIProvider`` /
  ``GeminiProvider``, chosen at runtime by :func:`resolve_provider_name` / :func:`get_model_provider`.
* **Adapter** — :class:`ProtocolAdapter`. The actual protocol conversion, *bidirectional*: the
  canonical (Anthropic) request → a vendor request (``adapt_request``), and the vendor's stream →
  canonical Anthropic-shaped parts (``adapt_stream``). ``OpenAIAdapter`` / ``GeminiAdapter`` implement
  it; Anthropic needs none (it already speaks the canonical protocol — an identity passthrough). A
  Strategy *composes* an Adapter, so "reach the backend" and "convert the protocol" stay separate.
* **Facade** — :class:`ModelGateway`. One tiny surface (``get_client`` / ``open_stream``) that hides
  all of the above from the caller: it never learns which provider or adapter is in play.

Selection: ``TABVIS_MODEL_PROVIDER`` wins (``anthropic`` | ``openai`` | ``gemini``); otherwise inferred
from the model id (``gpt*``/``o1``/``o3``/``o4``/``chatgpt`` → openai, ``gemini*`` → gemini, else
anthropic). The OpenAI/Gemini SDKs are optional extras — a strategy raises a clear install hint if
its package is missing, never a silent downgrade.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

VALID_PROVIDERS = ("anthropic", "openai", "gemini")


# --------------------------------------------------------------------------- Adapter (pattern)


@runtime_checkable
class ProtocolAdapter(Protocol):
    """ADAPTER: bidirectional conversion between the canonical protocol and one vendor's.

    ``adapt_request``  — canonical (Anthropic) request params → the vendor's request.
    ``adapt_stream``   — the vendor's response stream → canonical Anthropic-shaped stream parts.
    """

    name: str

    def adapt_request(self, params: dict[str, Any]) -> Any: ...

    def adapt_stream(self, vendor_stream: Any, *, model: str) -> AsyncIterator[Any]: ...


# --------------------------------------------------------------------------- Strategy (pattern)


@runtime_checkable
class ModelProvider(Protocol):
    """STRATEGY: an interchangeable backend. Builds a client and opens an Anthropic-shaped stream.

    A concrete strategy composes a :class:`ProtocolAdapter` (except Anthropic, which is a passthrough).
    """

    name: str

    async def get_client(self) -> Any: ...

    async def create_stream(self, client: Any, params: dict[str, Any]) -> AsyncIterator[Any]:
        """Send ``params`` (canonical) and return an async-iterable of canonical stream parts."""
        ...


def resolve_provider_name(model: str | None) -> str:
    """Which provider drives ``model`` — explicit env override, else inferred from the id."""
    env = (os.environ.get("TABVIS_MODEL_PROVIDER") or "").strip().lower()
    if env in VALID_PROVIDERS:
        return env
    m = (model or "").strip().lower()
    head, _, _ = m.partition("/")  # strip an explicit "provider/model" prefix
    if head in VALID_PROVIDERS:
        return head
    if m.startswith(("gpt", "chatgpt", "o1", "o3", "o4")):
        return "openai"
    if m.startswith("gemini") or m.startswith("models/gemini"):
        return "gemini"
    return "anthropic"


def get_model_provider(model: str | None, source: str | None = None) -> ModelProvider:
    """The concrete STRATEGY for ``model`` (lazily imported so optional SDKs stay optional)."""
    name = resolve_provider_name(model)
    if name == "openai":
        from tabvis.agent.api.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(source=source)
    if name == "gemini":
        from tabvis.agent.api.providers.gemini_provider import GeminiProvider

        return GeminiProvider(source=source)
    from tabvis.agent.api.providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(source=source, model=model)


def strip_provider_prefix(model: str | None) -> str:
    """``openai/gpt-4o`` → ``gpt-4o``; a bare id is returned unchanged."""
    m = (model or "").strip()
    head, sep, rest = m.partition("/")
    if sep and head.lower() in VALID_PROVIDERS:
        return rest
    return m


# --------------------------------------------------------------------------- Facade (pattern)


class ModelGateway:
    """FACADE over the model subsystem: one surface hiding strategy selection, the vendor client,
    and protocol adaptation. The caller does ``get_client()`` then ``open_stream(client, params)``
    and never learns which provider or adapter is involved — so the retry loop stays provider-blind.

    (``get_client`` and ``open_stream`` are kept separate, rather than one call, because the retry
    machinery rebuilds the client per attempt and treats the stream open as the retryable unit.)
    """

    def __init__(self, model: str | None, source: str | None = None) -> None:
        self._provider: ModelProvider = get_model_provider(model, source)

    @property
    def provider_name(self) -> str:
        return self._provider.name

    async def get_client(self) -> Any:
        return await self._provider.get_client()

    async def open_stream(self, client: Any, params: dict[str, Any]) -> AsyncIterator[Any]:
        return await self._provider.create_stream(client, params)


def get_model_gateway(model: str | None, source: str | None = None) -> ModelGateway:
    """The FACADE for ``model`` — the one entry the rest of the app uses."""
    return ModelGateway(model, source)
