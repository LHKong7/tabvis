"""Vision-capability gate + OCR fallback for non-multimodal models.

Covers get_model_supports_vision, and the send-path transform that swaps image blocks for OCR text
(or a note) when the active model has no vision. Uses asyncio.run — the repo has no pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tabvis.agent.api.model_client import _replace_images_for_nonvision_model
from tabvis.utils import ocr
from tabvis.utils.model.model import get_model_supports_vision


@pytest.mark.parametrize(
    ("model", "provider", "expected"),
    [
        ("gpt-4o", "openai", True),
        ("gpt-4o-mini", "openai", True),
        ("gpt-4-turbo", "openai", True),
        ("o3", "openai", True),
        ("gpt-3.5-turbo", "openai", False),
        ("deepseek-chat", "openai", False),  # custom text-only endpoint via provider=openai
        ("gemini-1.5-pro", "gemini", True),
        ("gemini-2.0-flash", "gemini", True),
        ("gemini-1.0-pro", "gemini", False),
        ("claude-sonnet-4-6", "anthropic", True),
        ("claude-3-5-sonnet-20241022", "anthropic", True),
        ("claude-2.1", "anthropic", False),
        ("claude-instant-1.2", "anthropic", False),
    ],
)
def test_supports_vision(model: str, provider: str, expected: bool, monkeypatch) -> None:
    monkeypatch.delenv("TABVIS_MODEL_SUPPORTS_VISION", raising=False)
    assert get_model_supports_vision(model, provider) is expected


def test_vision_override(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_MODEL_SUPPORTS_VISION", "0")
    assert get_model_supports_vision("gpt-4o", "openai") is False  # forced off
    monkeypatch.setenv("TABVIS_MODEL_SUPPORTS_VISION", "1")
    assert get_model_supports_vision("gpt-3.5-turbo", "openai") is True  # forced on


def _img(data: str = "aGVsbG8=") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _messages() -> list[dict]:
    # A top-level pasted image AND an image nested in a tool_result (browser/MCP screenshot shape).
    return [
        {"role": "user", "content": [{"type": "text", "text": "hi"}, _img("AAAA")]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "shot"}, _img("BBBB")]}
            ],
        },
    ]


def test_vision_model_left_untouched(monkeypatch) -> None:
    monkeypatch.delenv("TABVIS_MODEL_SUPPORTS_VISION", raising=False)
    msgs = _messages()
    out = asyncio.run(_replace_images_for_nonvision_model(msgs, "gpt-4o"))
    assert out is msgs  # same object — no copy, no transform


def test_nonvision_ocr_replaces_all_images(monkeypatch) -> None:
    monkeypatch.delenv("TABVIS_MODEL_SUPPORTS_VISION", raising=False)
    monkeypatch.setattr(ocr, "ocr_enabled", lambda: True)
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image_base64", lambda data, media_type="image/png", lang=None: f"EXTRACTED-{data}")

    out = asyncio.run(_replace_images_for_nonvision_model(_messages(), "gpt-3.5-turbo"))

    top = out[0]["content"]
    assert top[1]["type"] == "text" and "EXTRACTED-AAAA" in top[1]["text"]
    nested = out[1]["content"][0]["content"]
    assert nested[1]["type"] == "text" and "EXTRACTED-BBBB" in nested[1]["text"]
    assert '"type": "image"' not in json.dumps(out)  # no image block survives


def test_nonvision_no_engine_strips_images(monkeypatch) -> None:
    monkeypatch.delenv("TABVIS_MODEL_SUPPORTS_VISION", raising=False)
    monkeypatch.setattr(ocr, "ocr_enabled", lambda: True)
    monkeypatch.setattr(ocr, "ocr_available", lambda: False)

    out = asyncio.run(_replace_images_for_nonvision_model(_messages(), "gpt-3.5-turbo"))
    block = out[0]["content"][1]
    assert block["type"] == "text" and "omitted" in block["text"]
    assert '"type": "image"' not in json.dumps(out)


def test_nonvision_history_not_mutated(monkeypatch) -> None:
    """The original messages keep their image blocks (a later vision model still sees them)."""
    monkeypatch.delenv("TABVIS_MODEL_SUPPORTS_VISION", raising=False)
    monkeypatch.setattr(ocr, "ocr_enabled", lambda: True)
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image_base64", lambda data, media_type="image/png", lang=None: "X")

    msgs = _messages()
    asyncio.run(_replace_images_for_nonvision_model(msgs, "gpt-3.5-turbo"))
    assert msgs[0]["content"][1]["type"] == "image"  # untouched original
    assert msgs[1]["content"][0]["content"][1]["type"] == "image"


def test_ocr_available_returns_bool() -> None:
    ocr._reset_caches_for_test()
    assert isinstance(ocr.ocr_available(), bool)
    assert isinstance(ocr.available_engines(), list)


def test_ocr_enabled_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("TABVIS_OCR_ENABLED", raising=False)
    assert ocr.ocr_enabled() is True
    monkeypatch.setenv("TABVIS_OCR_ENABLED", "0")
    assert ocr.ocr_enabled() is False
    monkeypatch.setenv("TABVIS_OCR_ENABLED", "1")
    assert ocr.ocr_enabled() is True
