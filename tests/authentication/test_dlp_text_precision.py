"""DLP redaction must not destroy the data it is scanning.

``_deep_clean`` runs these over every outbound payload, so an over-broad pattern silently deletes
the agent's own results: token counts read as credentials, and a phone pattern that crossed line
breaks merged whole tables into one ``[redacted]``.
"""

from __future__ import annotations

import pytest

from tabvis.dlp.text import REDACTED, mask_identifiers, redact_mapping

ACCOUNTING_KEYS = [
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "inputTokens",
    "maxTokens",
    "max_output_tokens",
    "token_count",
    "tokenizer",
]

CREDENTIAL_KEYS = [
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "refresh_tokens",
    "tokenValue",
    "session_storage",
    "password",
]


@pytest.mark.parametrize("key", ACCOUNTING_KEYS)
def test_token_accounting_keys_survive(key: str) -> None:
    assert redact_mapping({key: 1234})[key] == 1234


@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_credential_keys_are_still_redacted(key: str) -> None:
    assert redact_mapping({key: "real-secret"})[key] == REDACTED


def test_phone_masking_never_spans_a_line_break() -> None:
    """Independent rows must stay independent; merging them deleted whole extracted tables."""
    table = "Revenue by year\n2021 4820193\n2022 5910442\n2023 6733120\nEND"
    masked = mask_identifiers(table)
    assert masked.count("\n") == table.count("\n")
    assert masked.startswith("Revenue by year\n")
    assert masked.endswith("\nEND")


@pytest.mark.parametrize(
    "text", ["call me at +1 415-555-0134 tomorrow", "+44 20 7946 0958"]
)
def test_real_phone_numbers_are_still_masked(text: str) -> None:
    assert REDACTED in mask_identifiers(text)


def test_iso_dates_are_not_identifiers() -> None:
    assert mask_identifiers("published 2026-07-27 today") == "published 2026-07-27 today"
