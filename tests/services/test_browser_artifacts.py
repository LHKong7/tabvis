"""Tests for the browser artifacts store (tabvis.browser.artifacts).

The store records the agent's browsing trail — navigation / page metadata / interaction / DOM — under
the per-session dir. These tests exercise the event log + DOM content-addressing + redaction + the
read API without a live browser (DOM capture returns "" when no BrowserService is around, so the
event-log path is deterministic; the content-addressing is tested directly).

``config_home`` (autouse from tests/conftest) roots the session dir in a tmp dir; each test switches
to its own session id so artifacts never collide, and the module's seq counter is reset.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from tabvis.bootstrap.state import switch_session
from tabvis.browser import artifacts as A


@pytest.fixture(autouse=True)
def _fresh_session(request: pytest.FixtureRequest) -> Any:
    switch_session(f"sess-{request.node.name}")
    A._seq_by_dir.clear()
    yield
    A._seq_by_dir.clear()


def _record(event: dict[str, Any], data: dict[str, Any]) -> None:
    asyncio.run(A.record_browser_artifact(event, data))


# --------------------------------------------------------------------------- event log


def test_records_navigation_interaction_and_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")  # no live browser in tests
    _record({"type": "navigation", "action": "goto", "url": "https://a.com"}, {"url": "https://a.com", "title": "A", "tab_count": 1})
    _record({"type": "interaction", "action": "click", "interaction": {"ref": "e5", "double": False}}, {"url": "https://a.com", "title": "A", "tab_count": 1})
    _record({"type": "page", "action": "snapshot"}, {"url": "https://a.com/x", "title": "X", "tab_count": 2})

    events = A.load_artifacts()
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["type"] for e in events] == ["navigation", "interaction", "page"]
    assert events[0]["action"] == "goto" and events[0]["url"] == "https://a.com"
    assert events[1]["interaction"] == {"ref": "e5", "double": False}
    assert events[2]["title"] == "X" and events[2]["tab_count"] == 2
    assert all(e.get("dom_ref") is None for e in events)  # DOM disabled


def test_disabled_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS", "0")
    _record({"type": "navigation", "action": "goto", "url": "https://a.com"}, {"url": "https://a.com"})
    assert A.load_artifacts() == []
    assert not os.path.exists(A.events_path())


def test_summary_counts_by_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    _record({"type": "navigation", "action": "goto", "url": "https://a.com"}, {"url": "https://a.com"})
    _record({"type": "navigation", "action": "reload"}, {"url": "https://a.com"})
    _record({"type": "page", "action": "snapshot"}, {"url": "https://a.com"})
    s = A.artifacts_summary()
    assert s["count"] == 3
    assert s["by_type"] == {"navigation": 2, "page": 1}
    assert s["last_url"] == "https://a.com"


# --------------------------------------------------------------------------- interaction redaction


def test_type_text_truncated_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    long = "x" * (A._MAX_INPUT_CHARS + 50)
    _record({"type": "interaction", "action": "type", "interaction": {"ref": "e1", "text": long}}, {"url": "u"})
    inter = A.load_artifacts()[0]["interaction"]
    assert len(inter["text"]) == A._MAX_INPUT_CHARS and inter["text_truncated"] is True


def test_redact_input_hides_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_REDACT_INPUT", "1")
    _record({"type": "interaction", "action": "type", "interaction": {"ref": "e1", "text": "hunter2"}}, {"url": "u"})
    inter = A.load_artifacts()[0]["interaction"]
    assert "text" not in inter and inter["text_redacted"] is True and inter["text_len"] == 7


# --------------------------------------------------------------------------- DOM content-addressing


def test_dom_is_content_addressed_and_deduped() -> None:
    directory = A.get_artifacts_dir()
    os.makedirs(directory, exist_ok=True)
    r1, n1 = A._store_dom_sync(directory, "<html>same</html>")
    r2, _ = A._store_dom_sync(directory, "<html>same</html>")
    r3, _ = A._store_dom_sync(directory, "<html>different</html>")
    assert r1 == r2 and r3 != r1            # identical DOM => one file
    assert n1 == len("<html>same</html>".encode())
    # one shared file for the two identical writes, plus the different one
    dom_files = os.listdir(os.path.join(directory, A.DOM_SUBDIR))
    assert len(dom_files) == 2
    assert A.read_dom(r1) == "<html>same</html>"


def test_read_dom_rejects_path_traversal() -> None:
    assert A.read_dom("../../etc/passwd") is None
    assert A.read_dom("../browser-session.json") is None
    assert A.read_dom("") is None


# --------------------------------------------------------------------------- config


def test_config_accessors(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.utils import browser_config as bc

    assert bc.is_browser_artifacts_enabled() is True         # default on
    assert bc.is_browser_artifacts_dom_enabled() is True
    assert bc.is_browser_artifacts_redact_input() is False
    assert bc.get_browser_artifacts_max_dom_bytes() == 1_000_000

    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS", "0")
    assert bc.is_browser_artifacts_enabled() is False
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_MAX_DOM_BYTES", "2048")
    assert bc.get_browser_artifacts_max_dom_bytes() == 2048


def test_events_are_valid_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    _record({"type": "navigation", "action": "goto", "url": "https://a.com"}, {"url": "https://a.com"})
    with open(A.events_path(), encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["url"] == "https://a.com"
