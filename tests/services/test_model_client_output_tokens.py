from __future__ import annotations

from tabvis.agent.api.model_client import get_max_output_tokens


def test_output_tokens_default_to_selected_model_capability(monkeypatch) -> None:
    monkeypatch.delenv("TABVIS_MAX_OUTPUT_TOKENS", raising=False)

    assert get_max_output_tokens("claude-sonnet-4-6") == 32_000
    assert get_max_output_tokens("claude-3-opus") == 4_096


def test_output_tokens_explicit_environment_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_MAX_OUTPUT_TOKENS", "12345")

    assert get_max_output_tokens("claude-sonnet-4-6") == 12_345


def test_invalid_output_tokens_override_falls_back_to_model_default(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_MAX_OUTPUT_TOKENS", "not-a-number")

    assert get_max_output_tokens("claude-sonnet-4-6") == 32_000
