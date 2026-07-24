"""Unified DLP Gateway + cleaners (design §11.1, §11.2, §11.3, §16.1)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tabvis.authentication.models import CredentialCapability, ResolvedCredentials
from tabvis.authentication.secrets import secret_from_str
from tabvis.dlp import canary
from tabvis.dlp.gateway import DLPGateway
from tabvis.dlp.text import mask_identifiers, redact_headers, redact_mapping
from tabvis.dlp.url import clean_url


@pytest.fixture(autouse=True)
def _clean():
    canary.clear()
    yield
    canary.clear()


# --------------------------------------------------------------------------- cleaners


def test_clean_url_strips_userinfo_fragment_query_values() -> None:
    out = clean_url("https://user:pass@ex.com/login?token=abc123&x=1#frag")
    assert "user" not in out and "pass" not in out
    assert "abc123" not in out and "#frag" not in out
    assert out.startswith("https://ex.com/login")
    assert "token=" in out and "x=" in out  # keys kept, values dropped


def test_redact_headers() -> None:
    out = redact_headers({"Cookie": "sid=1", "Authorization": "Bearer x", "Accept": "text/html"})
    assert out["Cookie"] == "[redacted]"
    assert out["Authorization"] == "[redacted]"
    assert out["Accept"] == "text/html"


def test_mask_identifiers() -> None:
    assert "alice@example.com" not in mask_identifiers("mail alice@example.com now")
    assert "555" not in mask_identifiers("call +1 555-123-4567 please")


def test_redact_mapping_sensitive_keys() -> None:
    out = redact_mapping({"password": "hunter2", "user": "alice", "nested": {"api_key": "k"}})
    assert out["password"] == "[redacted]"
    assert out["nested"]["api_key"] == "[redacted]"
    assert out["user"] == "alice"


# --------------------------------------------------------------------------- gateway


def test_gateway_passes_clean_payload() -> None:
    gw = DLPGateway()
    decision = gw.scrub("log", {"msg": "hello", "url": "https://ex.com/a?t=secret"})
    assert not decision.blocked
    assert decision.payload["url"] == "https://ex.com/a?t="


def test_gateway_blocks_on_canary() -> None:
    canary.register(b"CanarySecretValue1", tag="password:p1")
    blocked_events = []
    gw = DLPGateway(on_secret_blocked=blocked_events.append)
    decision = gw.scrub("model_request", {"page": "the token is CanarySecretValue1 haha"})
    assert decision.blocked
    assert decision.payload is None
    assert decision.fingerprint is not None
    # a dlp.secret_blocked event fired, containing only the one-way fingerprint (no secret)
    assert len(blocked_events) == 1
    assert blocked_events[0].event == "dlp.secret_blocked"
    assert "CanarySecretValue1" not in blocked_events[0].fingerprint


def test_gateway_blocks_secret_value_object() -> None:
    gw = DLPGateway()
    decision = gw.scrub("transcript", {"leak": secret_from_str("hunter2xyz")})
    assert decision.blocked
    assert decision.fingerprint == "forbidden-object"


def test_gateway_blocks_resolved_credentials_object() -> None:
    gw = DLPGateway()
    rc = ResolvedCredentials(password=secret_from_str("hunter2xyz"))
    assert gw.scrub("artifact", rc).blocked


def test_gateway_blocks_capability_object() -> None:
    gw = DLPGateway()
    cap = CredentialCapability(
        id="cap_1",
        credential_profile_id="p1",
        browser_session_id="b1",
        task_id="t1",
        user_id="u1",
        top_level_origin="https://x.com",
        frame_origin="https://x.com",
        page_id="pg",
        navigation_generation=1,
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
    )
    assert gw.scrub("telemetry", {"cap": cap}).blocked


def test_gateway_redacts_headers_and_cookies() -> None:
    gw = DLPGateway()
    decision = gw.scrub("api", {"Cookie": "sid=abc", "Authorization": "Bearer t", "path": "/x"})
    assert not decision.blocked
    assert decision.payload["Cookie"] == "[redacted]"
    assert decision.payload["Authorization"] == "[redacted]"


def test_block_hook_failure_does_not_unblock() -> None:
    canary.register(b"AnotherCanary9", tag="x")

    def boom(_event):
        raise RuntimeError("hook down")

    gw = DLPGateway(on_secret_blocked=boom)
    decision = gw.scrub("crash_report", "leak AnotherCanary9 here")
    assert decision.blocked  # a broken hook still results in a block, never a passthrough
