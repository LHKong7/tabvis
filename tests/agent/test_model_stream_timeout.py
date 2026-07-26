"""Progress-aware model-stream watchdog and timeout-specific retry budget."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.agent.api import with_retry as retry_mod
from tabvis.agent.api.errors import get_assistant_message_from_error
from tabvis.agent.api.model_client import _drain_stream_with_watchdog
from tabvis.agent.api.stream_timeout import ModelStreamTimeoutError
from tabvis.agent.api.with_retry import (
    CannotRetryError,
    RetryError,
    RetryOptions,
    with_retry,
)
from tabvis.utils.thinking import DISABLED_THINKING


class _TimedStream:
    def __init__(self, steps: list[tuple[float, Any]]) -> None:
        self.steps = steps
        self.closed = False

    def __aiter__(self):
        async def generate():
            for delay, part in self.steps:
                await asyncio.sleep(delay)
                yield part

        return generate()

    async def aclose(self) -> None:
        self.closed = True


def _part(kind: str, **fields: Any) -> Any:
    return SimpleNamespace(type=kind, **fields)


def test_empty_heartbeats_do_not_reset_first_output_deadline() -> None:
    async def scenario() -> None:
        stream = _TimedStream(
            [
                (0.005, _part("message_start", message={})),
                (0.005, _part("ping")),
                (0.005, _part("ping")),
                (0.100, _part("ping")),
            ]
        )
        with pytest.raises(ModelStreamTimeoutError) as caught:
            await _drain_stream_with_watchdog(
                stream,
                first_event_timeout_seconds=0.04,
                idle_timeout_seconds=0.04,
                attempt=1,
            )
        assert caught.value.phase == "first_event"
        assert stream.closed is True

    asyncio.run(scenario())


def test_meaningful_deltas_reset_idle_deadline() -> None:
    async def scenario() -> None:
        stream = _TimedStream(
            [
                (0, _part("message_start", message={})),
                (
                    0.01,
                    _part(
                        "content_block_delta",
                        index=0,
                        delta=SimpleNamespace(type="text_delta", text="a"),
                    ),
                ),
                (
                    0.025,
                    _part(
                        "content_block_delta",
                        index=0,
                        delta=SimpleNamespace(type="text_delta", text="b"),
                    ),
                ),
                (0.025, _part("message_stop")),
            ]
        )
        parts = await _drain_stream_with_watchdog(
            stream,
            first_event_timeout_seconds=0.04,
            idle_timeout_seconds=0.04,
            attempt=1,
        )
        assert [part.type for part in parts][-1] == "message_stop"
        assert stream.closed is False

    asyncio.run(scenario())


def test_timeout_uses_one_retry_not_generic_retry_budget(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_MODEL_TIMEOUT_RETRIES", "1")
    calls = 0

    async def no_sleep(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(retry_mod, "_sleep", no_sleep)

    async def get_client() -> object:
        return object()

    async def operation(_client: Any, attempt: int, _context: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ModelStreamTimeoutError(
            phase="first_event", timeout_seconds=45, attempt=attempt
        )

    async def scenario() -> None:
        statuses: list[RetryError] = []
        with pytest.raises(CannotRetryError) as caught:
            async for item in with_retry(
                get_client,
                operation,
                RetryOptions(
                    model="test-model",
                    thinking_config=DISABLED_THINKING,
                    max_retries=10,
                ),
            ):
                assert isinstance(item, RetryError)
                statuses.append(item)
        assert isinstance(caught.value.original_error, ModelStreamTimeoutError)
        assert len(statuses) == 1
        assert statuses[0].message["retryAttempt"] == 1
        assert statuses[0].message["maxRetries"] == 1

    asyncio.run(scenario())
    assert calls == 2


def test_timeout_maps_to_resume_safe_error_code() -> None:
    message = get_assistant_message_from_error(
        ModelStreamTimeoutError(
            phase="idle", timeout_seconds=60, attempt=2
        ),
        "test-model",
    )
    assert message["apiError"] == "model_stream_timeout"
    assert message["error"] == "model_stream_timeout"
    assert "browser state were preserved" in message["message"]["content"][0]["text"]
