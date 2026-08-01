"""Model-stream timeout policy and typed error.

The provider SDK request timeout is only a last-resort transport ceiling. Agent runs need a much
shorter, progress-aware watchdog: gateways may keep an SSE connection alive with empty chunks or
heartbeats while the model produces no usable output. This module is dependency-light so the model
client, retry layer, and error formatter can share one timeout identity without import cycles.
"""

from __future__ import annotations

import os

DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS = 45.0
DEFAULT_IDLE_TIMEOUT_SECONDS = 60.0
DEFAULT_LARGE_CONTEXT_TIMEOUT_SECONDS = 120.0
DEFAULT_TIMEOUT_RETRIES = 1

# Roughly 100k tokens for predominantly Latin text. This is deliberately conservative: large
# prompts receive more prefill time, while ordinary browser/tool turns fail fast enough to remain
# interactive.
LARGE_CONTEXT_THRESHOLD_CHARS = 400_000


class ModelStreamTimeoutError(TimeoutError):
    """A model stream made no meaningful progress before its watchdog deadline."""

    code = "model_stream_timeout"

    def __init__(self, *, phase: str, timeout_seconds: float, attempt: int) -> None:
        self.phase = phase
        self.timeout_seconds = timeout_seconds
        self.attempt = attempt
        label = "first meaningful model output" if phase == "first_event" else "model stream progress"
        super().__init__(
            f"Timed out waiting {timeout_seconds:g}s for {label} (attempt {attempt})"
        )


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return default


def get_first_event_timeout_seconds(estimated_input_chars: int) -> float:
    """First meaningful-output deadline, enlarged for genuinely large contexts."""
    explicit = os.environ.get("TABVIS_MODEL_FIRST_EVENT_TIMEOUT_SECONDS")
    if explicit:
        return _positive_float(
            "TABVIS_MODEL_FIRST_EVENT_TIMEOUT_SECONDS",
            DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS,
        )
    if estimated_input_chars >= LARGE_CONTEXT_THRESHOLD_CHARS:
        return _positive_float(
            "TABVIS_MODEL_LARGE_CONTEXT_TIMEOUT_SECONDS",
            DEFAULT_LARGE_CONTEXT_TIMEOUT_SECONDS,
        )
    return DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS


def get_idle_timeout_seconds() -> float:
    """Meaningful-progress idle deadline, honoring the previous env name as a fallback."""
    if os.environ.get("TABVIS_MODEL_IDLE_TIMEOUT_SECONDS"):
        return _positive_float(
            "TABVIS_MODEL_IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT_SECONDS
        )
    return _positive_float("TABVIS_STREAM_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT_SECONDS)


def get_timeout_retries() -> int:
    """Number of retries reserved specifically for model timeouts (clamped to 0..3)."""
    raw = os.environ.get("TABVIS_MODEL_TIMEOUT_RETRIES")
    if raw:
        try:
            return max(0, min(int(raw), 3))
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_RETRIES
