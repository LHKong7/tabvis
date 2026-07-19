"""Tests for the snapshot heuristics in ``tabvis.browser.browser_service``.

The live behaviour (public ``aria_snapshot(mode='ai')`` producing refs, refs resolving, and a canvas
page triggering the screenshot+HTML supplement) is exercised against a real browser elsewhere. These
are the pure, browser-free pieces: the "is the accessibility tree too sparse to reason from?" gate
that decides whether observe() attaches a screenshot + HTML.
"""

from __future__ import annotations

from tabvis.browser.browser_service import _aria_is_thin

# A realistic AI-mode aria snapshot for an ordinary form page — plenty of refs and text.
_RICH = """- generic [active] [ref=e1]:
  - heading "Sign in" [level=1] [ref=e2]
  - paragraph [ref=e3]: Please sign in to your account to continue.
  - textbox "Email" [ref=e4]
  - textbox "Password" [ref=e5]
  - button "Sign in" [ref=e6]
  - link "Forgot password?" [ref=e7]
"""


def test_empty_snapshot_is_thin() -> None:
    assert _aria_is_thin("") is True
    assert _aria_is_thin("   \n  ") is True


def test_rich_snapshot_is_not_thin() -> None:
    """A page the aria tree describes well must NOT trigger the screenshot+HTML supplement."""
    assert _aria_is_thin(_RICH) is False


def test_canvas_like_snapshot_is_thin() -> None:
    """A near-empty tree (a canvas/visual page) is 'not enough' and should be supplemented."""
    assert _aria_is_thin("- generic [ref=e1]") is True
    assert _aria_is_thin(
        "(the page appears to be empty — it may still be loading; try BrowserWait)"
    ) is True


def test_long_text_but_few_refs_is_not_thin() -> None:
    """An article: little interactivity, but lots to READ — aria is enough, so not thin."""
    article = "- article [ref=e1]:\n  - paragraph [ref=e2]: " + ("word " * 200)
    assert len(article) >= 400
    assert _aria_is_thin(article) is False


def test_many_refs_short_text_is_not_thin() -> None:
    """A dense toolbar (many refs, little prose) is still actionable — not thin."""
    dense = "\n".join(f'- button "b{i}" [ref=e{i}]' for i in range(1, 8))
    assert dense.count("[ref=") > 3
    assert _aria_is_thin(dense) is False
