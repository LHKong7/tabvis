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


def test_records_native_execution_and_pacing_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    _record(
        {
            "type": "interaction",
            "action": "click",
            "interaction": {"ref": "e5"},
        },
        {
            "url": "https://a.com",
            "action_result": {
                "executed_via": "cdp",
                "pacing_wait_ms": 187,
                "verified": True,
                "coordinates": {"x": 10, "y": 20},
            },
        },
    )
    assert A.load_artifacts()[0]["execution"] == {
        "executed_via": "cdp",
        "pacing_wait_ms": 187,
        "verified": True,
    }


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
    assert s["research_checkpoints"] == 0
    assert s["last_url"] == "https://a.com"


# --------------------------------------------------------------------------- research checkpoints


def test_extract_persists_structured_research_and_renders_latest_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    base = {
        "url": "https://example.test/security/report",
        "title": "Security report",
        "query": "2026",
        "scope": "main",
        "scope_matched": True,
        "dates": ["2026-07-26"],
        "headings": [{"level": 1, "text": "Results"}],
        "links": [{"text": "Dataset", "href": "https://example.test/data"}],
        "tables": [{"caption": "Metrics", "rows": [["Detection", "37.5%"]]}],
    }
    _record(
        {"type": "page", "action": "extract"},
        {**base, "text": "Initial result."},
    )
    _record(
        {"type": "page", "action": "extract"},
        {**base, "text": "Corrected result: patch rate 23.4%."},
    )

    events = A.load_artifacts()
    assert events[-1]["research"]["dates"] == ["2026-07-26"]
    assert events[-1]["research"]["tables"][0]["rows"][0] == ["Detection", "37.5%"]
    assert A.artifacts_summary()["research_checkpoints"] == 2

    context = A.render_research_evidence_context()
    assert context is not None
    assert context.count("URL: https://example.test/security/report") == 1
    assert "Corrected result: patch rate 23.4%." in context
    assert "Initial result." not in context
    assert A.events_path() in context
    assert "LOW-PRIVILEGE EXTERNAL DATA" in context


def test_research_checkpoint_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    _record(
        {"type": "page", "action": "extract"},
        {
            "url": "https://example.test/large",
            "title": "Large",
            "text": "x" * 100_000,
            "links": [
                {"text": "link", "href": f"https://example.test/{index}"}
                for index in range(200)
            ],
        },
    )

    research = A.load_research_evidence()[0]
    assert len(research["text"]) <= A._MAX_RESEARCH_TEXT_CHARS
    assert len(json.dumps(research)) <= A._MAX_RESEARCH_EVENT_CHARS


def test_pdf_page_ranges_are_distinct_durable_research_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    path = os.path.join(A.get_artifacts_dir(), "paper.pdf")
    asyncio.run(
        A.record_pdf_research_artifact(
            {
                "url": "https://example.test/paper.pdf",
                "file_path": path,
                "pages": "1-20",
                "page_count": 35,
                "first_page": 1,
                "last_page": 20,
                "coverage_complete": False,
                "cumulative_page_ranges": ["1-20"],
                "next_pages": "21-35",
                "text_truncated": False,
                "text": "Methods and experimental setup.",
            }
        )
    )
    asyncio.run(
        A.record_pdf_research_artifact(
            {
                "url": "https://example.test/paper.pdf",
                "file_path": path,
                "pages": "21-35",
                "page_count": 35,
                "first_page": 21,
                "last_page": 35,
                "coverage_complete": True,
                "cumulative_page_ranges": ["1-35"],
                "next_pages": None,
                "text_truncated": False,
                "text": "Results, limitations, and appendices.",
            }
        )
    )

    evidence = A.load_research_evidence()
    assert [item["pages"] for item in evidence] == ["1-20", "21-35"]
    context = A.render_research_evidence_context()
    assert context is not None
    assert "PDF pages: 1-20 of 35" in context
    assert "PDF pages: 21-35 of 35" in context
    assert "cumulative_ranges=['1-35']" in context
    assert "coverage_complete=True" in context
    assert "Methods and experimental setup." in context
    assert "Results, limitations, and appendices." in context


def test_pdf_research_checkpoint_resolves_original_download_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture")
    asyncio.run(
        A.record_download_artifact(
            action="pdf_navigation",
            url="https://example.test/paper.pdf",
            path=str(path),
            filename="paper.pdf",
            policy_effect="allow",
        )
    )
    asyncio.run(
        A.record_pdf_research_artifact(
            {
                "file_path": str(path),
                "pages": "1-5",
                "page_count": 5,
                "coverage_complete": True,
                "text": "Full paper text.",
            }
        )
    )

    evidence = A.load_research_evidence()
    assert evidence[-1]["url"] == "https://example.test/paper.pdf"
    assert evidence[-1]["source_type"] == "pdf"


# --------------------------------------------------------------------------- interaction redaction


def test_text_redacted_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Secure default: the raw text is NOT persisted, only its length.
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    _record({"type": "interaction", "action": "type", "interaction": {"ref": "e1", "text": "hunter2"}}, {"url": "u"})
    inter = A.load_artifacts()[0]["interaction"]
    assert "text" not in inter and inter["text_redacted"] is True and inter["text_len"] == 7


def test_include_input_persists_truncated_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_INCLUDE_INPUT", "1")
    long = "hello world " * 60  # long, but ordinary prose (not sensitive-looking)
    _record({"type": "interaction", "action": "type", "interaction": {"ref": "e1", "text": long}}, {"url": "u"})
    inter = A.load_artifacts()[0]["interaction"]
    assert len(inter["text"]) == A._MAX_INPUT_CHARS and inter["text_truncated"] is True
    assert inter["text_len"] == len(long)


def test_redact_input_overrides_include(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_INCLUDE_INPUT", "1")
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_REDACT_INPUT", "1")
    _record({"type": "interaction", "action": "type", "interaction": {"ref": "e1", "text": "hunter2"}}, {"url": "u"})
    inter = A.load_artifacts()[0]["interaction"]
    assert "text" not in inter and inter["text_redacted"] is True and inter["text_len"] == 7


@pytest.mark.parametrize(
    "secret",
    [
        "4111111111111111",          # a Luhn-valid test card number
        "4111 1111 1111 1111",       # ...with the usual spacing
        "sk_live_0123456789abcdefghij",  # a long high-entropy token
    ],
)
def test_sensitive_text_redacted_even_with_include(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    # Card numbers / tokens are stripped unconditionally, even when inclusion is opted in.
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_INCLUDE_INPUT", "1")
    _record({"type": "interaction", "action": "type", "interaction": {"ref": "e1", "text": secret}}, {"url": "u"})
    inter = A.load_artifacts()[0]["interaction"]
    assert "text" not in inter
    assert inter["text_redacted"] is True and inter["text_redacted_reason"] == "sensitive"


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
    assert bc.is_browser_artifacts_include_input() is False   # secure default: text not persisted
    assert bc.is_browser_artifacts_redact_input() is False
    assert bc.get_browser_artifacts_max_dom_bytes() == 1_000_000

    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_INCLUDE_INPUT", "1")
    assert bc.is_browser_artifacts_include_input() is True

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
