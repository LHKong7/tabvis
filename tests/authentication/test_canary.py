"""DLP secret canary (design §11.3, §16.3 item 10)."""

from __future__ import annotations

import pytest

from tabvis.dlp import canary


@pytest.fixture(autouse=True)
def _clean() -> None:
    canary.clear()
    yield
    canary.clear()


def test_register_and_detect_whole_value() -> None:
    secret = "S3cr3tCanaryValue"
    fp = canary.register(secret.encode(), tag="password:p1")
    assert fp is not None
    assert canary.is_registered(secret.encode())
    assert not canary.is_registered(b"something-else")


def test_fingerprint_is_not_the_secret() -> None:
    secret = "S3cr3tCanaryValue"
    fp = canary.register(secret.encode(), tag="password:p1")
    assert secret not in fp
    assert fp is not None and len(fp) == 32  # 16-byte digest hex


def test_scan_text_finds_embedded_secret() -> None:
    secret = "CanaryLeak12345"
    canary.register(secret.encode(), tag="password:p1")
    # secret pasted inside a bigger blob (a URL, a DOM dump, a log line) is caught
    blob = f"GET /callback?token={secret}&x=1 HTTP/1.1"
    assert canary.scan_text(blob) is not None
    assert canary.scan_text("nothing to see here, totally clean") is None


def test_scan_tokens() -> None:
    secret = "TokenValueABCDEF"
    canary.register(secret.encode(), tag="cookie:sid")
    assert canary.scan_tokens(["sid=1", secret, "other"]) is not None
    assert canary.scan_tokens(["sid=1", "other"]) is None


def test_short_secrets_below_floor_are_skipped() -> None:
    # very short values would false-positive when substring-scanning arbitrary text
    assert canary.register(b"abc", tag="x") is None
    assert canary.is_registered(b"abc") is False


def test_clear_resets_registry() -> None:
    canary.register(b"LongEnoughSecret", tag="x")
    assert canary.registered_count() == 1
    canary.clear()
    assert canary.registered_count() == 0
    assert canary.scan_text("LongEnoughSecret") is None
