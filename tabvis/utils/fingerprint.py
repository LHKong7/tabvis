"""Tabvis attribution fingerprint

Computes a 3-character hex fingerprint stamped on requests for Tabvis attribution. The algorithm
is fixed by backend validation and MUST NOT change without coordinating with the model API
providers (see the TS source comment).

Casing: Python identifiers are snake_case; ``FINGERPRINT_SALT`` is an UPPER_CASE constant. The
messages passed in are internal transcript envelopes (plain dicts with wire keys), so this module
reads ``msg["type"]`` / ``msg["message"]["content"]`` / ``block["type"]`` / ``block["text"]`` by
their verbatim wire-key names.
"""

from __future__ import annotations

import hashlib
from typing import Any

from tabvis.bootstrap_macro import MACRO

# Hardcoded salt from backend validation. Must match exactly for fingerprint validation to pass.
FINGERPRINT_SALT = "59cf53e54c78"


def extract_first_message_text(messages: list[dict[str, Any]]) -> str:
    """Extract the text content from the first user message.

    Returns the first user message's text content, or ``""`` if there is no user message or it
    carries no text.
    """
    first_user_message = next(
        (msg for msg in messages if msg.get("type") == "user"), None
    )
    if first_user_message is None:
        return ""

    content = first_user_message.get("message", {}).get("content")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_block = next(
            (block for block in content if block.get("type") == "text"), None
        )
        if text_block is not None and text_block.get("type") == "text":
            return text_block.get("text", "")

    return ""


# Indices sampled from the first user message (fixed by backend validation — do not change).
_FINGERPRINT_INDICES = (4, 7, 20)

_ZERO_UNIT = "0".encode("utf-16-le")  # the UTF-16 code unit for the ``|| '0'`` fallback char.


def _sampled_chars(text: str) -> str:
    """JS ``msg[4]+msg[7]+msg[20]`` (with ``|| '0'`` fallback) as the exact JS string.

    JS strings index by UTF-16 code unit, and JS string concatenation re-pairs adjacent surrogate
    halves into a single astral character. To reproduce the JS string the backend hashes:

    1. collect the UTF-16 code unit at each index (or the ``'0'`` code unit when out of range),
    2. decode the concatenated UTF-16 buffer as a whole so adjacent surrogate halves recombine,
    3. normalize any remaining LONE surrogate to U+FFFD (matching how Node UTF-8-encodes a JS
       string that contains a lone surrogate).

    Indexing by Python code point instead would drift for any prompt with an emoji / astral char
    before index 20, breaking backend fingerprint validation — so this UTF-16 fidelity is required
    (same rationale as ``hash.djb2``'s code-unit iteration).
    """
    utf16 = text.encode("utf-16-le")
    buffer = bytearray()
    for index in _FINGERPRINT_INDICES:
        byte_offset = index * 2
        if byte_offset + 1 < len(utf16):
            buffer += utf16[byte_offset : byte_offset + 2]
        else:
            buffer += _ZERO_UNIT
    # Decode as a whole (adjacent surrogate halves recombine), then collapse any lone surrogate to
    # U+FFFD via a UTF-16 round-trip with ``replace`` (mirrors Node's lone-surrogate UTF-8 output).
    recombined = bytes(buffer).decode("utf-16-le", errors="surrogatepass")
    return recombined.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def compute_fingerprint(message_text: str, version: str) -> str:
    """Compute the 3-character fingerprint for Tabvis attribution.

    Algorithm: ``SHA256(SALT + msg[4] + msg[7] + msg[20] + version)[:3]``. IMPORTANT: do not
    change this method without careful coordination with supported model API providers.
    ``computeFingerprint``.
    """
    chars = _sampled_chars(message_text)
    fingerprint_input = f"{FINGERPRINT_SALT}{chars}{version}"
    # SHA256 over the UTF-8 bytes (Node ``createHash('sha256').update(str).digest('hex')``); return
    # the first 3 hex chars.
    digest = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
    return digest[:3]


def compute_fingerprint_from_messages(messages: list[dict[str, Any]]) -> str:
    """Compute the fingerprint from the first user message"""
    first_message_text = extract_first_message_text(messages)
    return compute_fingerprint(first_message_text, MACRO.VERSION)
