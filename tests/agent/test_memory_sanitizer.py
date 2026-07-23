"""Adversarial tests for the Agent Browser Memory sanitizer (Resume Plus design §9.2 / §13.4).

Phase 0 acceptance requires the deterministic URL sanitizer + excluded-origin matcher to cover query
tokens, userinfo, fragments, Unicode, and path escape. These exercise exactly those, plus typed-text
redaction and trust-class handling.
"""

from __future__ import annotations

import os

import pytest

from tabvis.agent.mem import sanitizer as S


# --------------------------------------------------------------------------- URL sanitization


def test_strips_userinfo_and_fragment() -> None:
    out = S.sanitize_url("https://user:pass@example.com/path?a=1#secret-fragment")
    assert out is not None
    assert out.origin == "https://example.com"
    assert out.path == "/path"
    assert "pass" not in out.safe_url and "#" not in out.safe_url


def test_drops_all_query_values_by_default() -> None:
    out = S.sanitize_url("https://example.com/s?token=abc123&q=hello")
    assert out is not None
    assert out.query_keys == ()          # nothing allowlisted
    assert out.dropped_query is True
    assert "abc123" not in out.safe_url and "hello" not in out.safe_url


def test_allowlisted_key_kept_without_value() -> None:
    out = S.sanitize_url("https://example.com/s?q=hello&page=2", allow_query_keys={"q", "page"})
    assert out is not None
    assert set(out.query_keys) == {"q", "page"}
    # keys survive but values never do, even for allowlisted keys
    assert "hello" not in out.safe_url and "2" not in out.safe_url
    assert "q=" in out.safe_url and "page=" in out.safe_url


def test_sensitive_key_never_kept_even_if_allowlisted() -> None:
    # An allowlist entry that is itself sensitive is still dropped.
    out = S.sanitize_url("https://example.com/s?access_token=x", allow_query_keys={"access_token"})
    assert out is not None
    assert out.query_keys == () and out.dropped_query is True


@pytest.mark.parametrize(
    "url",
    [
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "about:blank",
        "chrome://settings",
        "chrome-extension://abcd/page.html",
        "blob:https://example.com/uuid",
        "view-source:https://example.com",
    ],
)
def test_blocked_schemes_are_dropped(url: str) -> None:
    assert S.sanitize_url(url) is None


def test_missing_host_dropped() -> None:
    assert S.sanitize_url("https:///path") is None
    assert S.sanitize_url("") is None
    assert S.sanitize_url("not a url") is None


def test_port_preserved_userinfo_not() -> None:
    out = S.sanitize_url("https://user:pw@example.com:8443/x")
    assert out is not None and out.origin == "https://example.com:8443"


def test_unicode_host_and_path_normalized() -> None:
    out = S.sanitize_url("https://EXAMPLE.com/Café?x=1")
    assert out is not None
    assert out.origin == "https://example.com"  # host lowercased


# --------------------------------------------------------------------------- excluded origins


def test_exact_origin_excluded() -> None:
    pats = ["https://bank.example"]
    assert S.is_excluded_origin("https://bank.example/login", pats) is True
    assert S.is_excluded_origin("https://notbank.example/login", pats) is False


def test_wildcard_host_matches_apex_and_subdomains() -> None:
    pats = ["https://*.health.example"]
    assert S.is_excluded_origin("https://health.example/x", pats) is True
    assert S.is_excluded_origin("https://portal.health.example/x", pats) is True
    assert S.is_excluded_origin("https://a.b.health.example/x", pats) is True
    assert S.is_excluded_origin("https://evilhealth.example/x", pats) is False


def test_bare_host_pattern_and_scheme_agnostic() -> None:
    assert S.is_excluded_origin("http://bank.example/x", ["bank.example"]) is True
    # a scheme in the pattern constrains it
    assert S.is_excluded_origin("http://bank.example/x", ["https://bank.example"]) is False


def test_sanitize_url_drops_excluded_origin() -> None:
    assert S.sanitize_url("https://bank.example/statement", excluded_origins=["bank.example"]) is None


def test_get_excluded_origins_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY_EXCLUDE_ORIGINS", "https://a.example, b.example ,")
    assert S.get_excluded_origins() == ["https://a.example", "b.example"]


# --------------------------------------------------------------------------- typed text


def test_typed_text_reduced_to_length_and_kind() -> None:
    r = S.redact_typed_text("hunter2password")
    assert r.redacted is True and r.length == len("hunter2password")


@pytest.mark.parametrize(
    "text,kind",
    [
        ("", "empty"),
        ("4111111111111111", "secret_like"),           # long digit run
        ("sk_live_0123456789abcdefgh", "secret_like"),  # high-entropy token
        ("eyJhbGciOi.eyJzdWIi.sIg", "secret_like"),     # JWT-ish
        ("a@b.com", "email_like"),
        ("https://example.com", "url_like"),
        ("42", "numeric"),
        ("hello there", "text"),
    ],
)
def test_typed_text_classification(text: str, kind: str) -> None:
    assert S.classify_typed_text(text) == kind


# --------------------------------------------------------------------------- path escape


def test_safe_path_within_root(tmp_path: object) -> None:
    root = str(tmp_path)
    assert S.safe_evidence_path("dom/abc.html", root) == os.path.realpath(os.path.join(root, "dom/abc.html"))


def test_path_escape_rejected(tmp_path: object) -> None:
    root = str(tmp_path)
    assert S.safe_evidence_path("../../etc/passwd", root) is None
    assert S.safe_evidence_path("/etc/passwd", root) is None
    assert S.safe_evidence_path("", root) is None


# --------------------------------------------------------------------------- misc contracts


def test_title_truncated_and_normalized() -> None:
    assert S.sanitize_title("  Hello    World  ") == "Hello World"
    long = "x" * (S._MAX_TITLE_CHARS + 20)
    assert len(S.sanitize_title(long)) == S._MAX_TITLE_CHARS
    assert S.sanitize_title(None) is None


def test_trust_classes_exist() -> None:
    item = S.EvidenceItem(trust="web_content", kind="navigation",
                          url=S.sanitize_url("https://example.com/x"))
    assert item.trust == "web_content" and item.url is not None
