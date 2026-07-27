"""Transient provider-SDK errors must reach the retry layer, not kill the run on attempt 1.

``with_retry`` gates on ``anthropic.APIError``. The ``openai`` and ``google-genai`` SDKs raise
their own exception types, so without an explicit status check a 429/503 from an OpenAI- or
Gemini-backed endpoint fails that gate and raises ``CannotRetryError`` immediately, while the
identical anthropic status retries.
"""

from __future__ import annotations

import asyncio

import anthropic
import httpx
import pytest

from tabvis.agent.api.with_retry import (
    CannotRetryError,
    RetryOptions,
    RetryResult,
    with_retry,
)


class _OpenAIRateLimit(Exception):
    """Shaped like ``openai.RateLimitError``: carries ``status_code``."""

    status_code = 429


class _GeminiUnavailable(Exception):
    """Shaped like ``google.genai.errors.ServerError``: carries ``code``."""

    code = 503


class _OpenAIBadRequest(Exception):
    status_code = 400


def _anthropic_rate_limit() -> anthropic.RateLimitError:
    return anthropic.RateLimitError(
        "rate limit",
        response=httpx.Response(
            429,
            request=httpx.Request("POST", "http://model.invalid"),
            headers={"x-should-retry": "true"},
        ),
        body=None,
    )


def _drive(make_error, *, fail_times: int = 2) -> int:
    """Return the number of attempts made, or raise ``CannotRetryError``."""
    attempts = 0

    async def get_client():
        return object()

    async def operation(client, attempt, ctx):
        nonlocal attempts
        attempts += 1
        if attempts <= fail_times:
            raise make_error()
        return "ok"

    async def scenario() -> None:
        options = RetryOptions(model="m", thinking_config={"type": "disabled"})
        async for item in with_retry(get_client, operation, options):
            if isinstance(item, RetryResult):
                return

    asyncio.run(scenario())
    return attempts


@pytest.mark.parametrize(
    "make_error", [_anthropic_rate_limit, _OpenAIRateLimit, _GeminiUnavailable]
)
def test_transient_provider_errors_are_retried(make_error) -> None:
    assert _drive(make_error) == 3


@pytest.mark.parametrize("make_error", [_OpenAIBadRequest, RuntimeError])
def test_non_transient_errors_still_fail_fast(make_error) -> None:
    with pytest.raises(CannotRetryError):
        _drive(make_error)
