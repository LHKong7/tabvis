"""Anthropic-shaped stream-event objects the model-client loop consumes.

The whole query pipeline treats **Anthropic Messages/streaming as the canonical internal format**
(``model_client.query_model_with_streaming`` yields ``{'type':'stream_event','event': <part>}`` and
``query_engine`` reads ``event.type`` / ``event.message.usage`` / ``event.delta.stop_reason`` off
those parts). So a non-Anthropic provider does not invent a new event shape — it **translates** its
own SDK stream into these same parts, and everything downstream is unchanged.

The loop mixes dict-coercion (``_as_dict(part.message)``) with attribute access
(``getattr(part.delta, 'text')``), so one type must serve both: :class:`AttrDict` is a dict (so
``_as_dict`` and ``**spread`` work) that also exposes its keys as attributes (so ``.type`` / ``.text``
work), raising ``AttributeError`` on a missing key so ``getattr(x, 'y', default)`` still falls back.

The builders below produce exactly the six part types the loop handles; see model_client.py.
"""

from __future__ import annotations

from typing import Any


class AttrDict(dict):
    """A dict whose keys are also attributes (and which ``_as_dict`` returns as-is)."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e

    def model_dump(self) -> dict[str, Any]:  # so _as_dict()'s pydantic path is a no-op copy
        return dict(self)


def message_start(*, message_id: str, model: str, usage: dict[str, Any] | None = None) -> AttrDict:
    """A ``message_start`` part carrying the assistant Message skeleton (content filled by blocks)."""
    return AttrDict(
        type="message_start",
        message=AttrDict(
            id=message_id,
            type="message",
            role="assistant",
            model=model,
            content=[],
            stop_reason=None,
            stop_sequence=None,
            usage=AttrDict(usage or {}),
        ),
    )


def content_block_start(*, index: int, block: dict[str, Any]) -> AttrDict:
    return AttrDict(type="content_block_start", index=index, content_block=AttrDict(block))


def text_delta(*, index: int, text: str) -> AttrDict:
    return AttrDict(
        type="content_block_delta", index=index, delta=AttrDict(type="text_delta", text=text)
    )


def input_json_delta(*, index: int, partial_json: str) -> AttrDict:
    return AttrDict(
        type="content_block_delta",
        index=index,
        delta=AttrDict(type="input_json_delta", partial_json=partial_json),
    )


def thinking_delta(*, index: int, thinking: str) -> AttrDict:
    return AttrDict(
        type="content_block_delta", index=index, delta=AttrDict(type="thinking_delta", thinking=thinking)
    )


def content_block_stop(*, index: int) -> AttrDict:
    return AttrDict(type="content_block_stop", index=index)


def message_delta(*, stop_reason: str | None, usage: dict[str, Any] | None = None) -> AttrDict:
    return AttrDict(
        type="message_delta",
        delta=AttrDict(stop_reason=stop_reason, stop_sequence=None),
        usage=AttrDict(usage or {}),
    )


def message_stop() -> AttrDict:
    return AttrDict(type="message_stop")
