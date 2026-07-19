"""Zero-initialized usage object.

The WIDE 11-field runtime usage shape (see docs/SPINE_CONTRACTS.md). Kept as a plain dict
(NOT a forbid-extra model) because ``update_usage`` mutates it in place after a message is
yielded. ``empty_usage()`` returns a fresh deep copy each call.
"""

from __future__ import annotations

from typing import Any


def empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
        },
        "inference_geo": "",
        "iterations": [],
        "speed": "standard",
    }


# Module-level template (callers copy via empty_usage() to avoid shared-nested mutation).
EMPTY_USAGE: dict[str, Any] = empty_usage()
