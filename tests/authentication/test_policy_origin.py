"""Origin normalization & frame policy (design §8.1, §8.2, §16.1).

These are the anti-spoofing tests: homograph, trailing dot, userinfo, port, IDN, scheme downgrade,
and the ancestor-frame chain check.
"""

from __future__ import annotations

import pytest

from tabvis.authentication.policy import (
    OriginError,
    canonicalize_origin,
    frame_chain_authorized,
    origin_matches,
)


def test_default_port_normalized() -> None:
    assert canonicalize_origin("https://ex.com") == "https://ex.com"
    assert canonicalize_origin("https://ex.com:443") == "https://ex.com"
    assert canonicalize_origin("https://ex.com:8443") == "https://ex.com:8443"


def test_path_query_fragment_ignored() -> None:
    assert canonicalize_origin("https://ex.com/login?next=/a#frag") == "https://ex.com"


def test_host_lowercased() -> None:
    assert canonicalize_origin("https://EX.Com/Path") == "https://ex.com"


def test_trailing_dot_stripped() -> None:
    assert canonicalize_origin("https://ex.com.") == "https://ex.com"
    assert canonicalize_origin("https://ex.com.") == canonicalize_origin("https://ex.com")


def test_idn_punycode() -> None:
    # bücher.example → xn--bcher-kva.example
    assert canonicalize_origin("https://bücher.example") == "https://xn--bcher-kva.example"


def test_http_rejected() -> None:
    with pytest.raises(OriginError):
        canonicalize_origin("http://ex.com")


def test_userinfo_rejected() -> None:
    with pytest.raises(OriginError):
        canonicalize_origin("https://user:pass@ex.com")
    with pytest.raises(OriginError):
        canonicalize_origin("https://user@ex.com")


def test_non_https_schemes_rejected() -> None:
    for url in ("ftp://ex.com", "javascript:alert(1)", "data:text/html,x", "about:blank"):
        with pytest.raises(OriginError):
            canonicalize_origin(url)


def test_empty_rejected() -> None:
    with pytest.raises(OriginError):
        canonicalize_origin("")


def test_bad_port_rejected() -> None:
    with pytest.raises(OriginError):
        canonicalize_origin("https://ex.com:99999")


def test_origin_matches_exact_only() -> None:
    allowed = ["https://accounts.example.com"]
    assert origin_matches("https://accounts.example.com/login", allowed)
    assert origin_matches("https://accounts.example.com:443", allowed)
    # subdomain confusion / suffix attack must not match
    assert not origin_matches("https://accounts.example.com.attacker.test", allowed)
    assert not origin_matches("https://evil-accounts.example.com", allowed)
    assert not origin_matches("http://accounts.example.com", allowed)  # downgrade


def test_origin_matches_never_raises_on_garbage() -> None:
    assert origin_matches("not a url", ["https://ex.com"]) is False
    assert origin_matches("https://ex.com", ["also garbage"]) is False


def test_frame_chain_all_must_be_authorized() -> None:
    allowed = ["https://login.example.com", "https://example.com"]
    assert frame_chain_authorized(
        "https://login.example.com", ["https://example.com"], allowed
    )
    # one unauthorized ancestor fails the whole chain
    assert not frame_chain_authorized(
        "https://login.example.com", ["https://ads.evil.test"], allowed
    )
    # unauthorized input frame fails
    assert not frame_chain_authorized("https://evil.test", ["https://example.com"], allowed)
