"""Download audit artifacts (issue #5) and unexpected-download quarantine (issue #3).

Covers ``artifacts.record_download_artifact`` (the ``type=download`` audit event, storing a reference
+ hash, never the bytes) and ``BrowserService._save_download`` routing an unexpected download to the
workspace when policy allows, or to quarantine (out of the agent's reach) otherwise.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from tabvis.bootstrap.state import switch_session
from tabvis.browser import artifacts as A
from tabvis.browser import downloads
from tabvis.browser.browser_service import BrowserService


@pytest.fixture(autouse=True)
def _fresh_session(request: pytest.FixtureRequest) -> Any:
    switch_session(f"sess-{request.node.name}")
    A._seq_by_dir.clear()
    yield
    A._seq_by_dir.clear()


# --------------------------------------------------------------------------- #5 download artifacts


def test_download_artifact_records_reference_hash_not_content(tmp_path: Any) -> None:
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 hello")
    asyncio.run(
        A.record_download_artifact(
            action="explicit_download",
            url="https://x.test/report.pdf",
            path=str(f),
            policy_effect="allow",
            policy_rule_id="grant-1",
        )
    )
    events = A.load_artifacts()
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "download" and ev["action"] == "explicit_download"
    assert ev["url"] == "https://x.test/report.pdf"
    assert ev["path_ref"] == str(f) and ev["filename"] == "report.pdf"
    assert ev["size_bytes"] == len(b"%PDF-1.4 hello")
    assert ev["sha256"] and len(ev["sha256"]) == 64
    assert ev["policy_effect"] == "allow" and ev["policy_rule_id"] == "grant-1"
    # The file's bytes must never be embedded in the event log.
    assert "hello" not in open(A.events_path(), encoding="utf-8").read()


def test_download_artifact_marks_quarantine(tmp_path: Any) -> None:
    f = tmp_path / "sketchy.zip"
    f.write_bytes(b"PK\x03\x04")
    asyncio.run(
        A.record_download_artifact(
            action="click_download", url="https://evil.test/x.zip", path=str(f),
            policy_effect="deny", quarantined=True,
        )
    )
    ev = A.load_artifacts()[0]
    assert ev["quarantined"] is True and ev["policy_effect"] == "deny"


# --------------------------------------------------------------------------- #3 quarantine routing


class _FakeDownload:
    def __init__(self, url: str, name: str) -> None:
        self.url = url
        self.suggested_filename = name
        self.saved_to: str | None = None

    async def save_as(self, dest: str) -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"payload")
        self.saved_to = dest


def _run_save(svc: BrowserService, dl: _FakeDownload) -> None:
    asyncio.run(svc._save_download(dl))


def test_allowed_download_goes_to_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "trusted")  # download allowed
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS", "0")      # keep the test to the routing decision
    svc = BrowserService()
    dl = _FakeDownload("https://ok.test/a.zip", "a.zip")
    _run_save(svc, dl)
    assert dl.saved_to is not None
    assert os.path.commonpath([dl.saved_to, downloads.get_workspace_dir()]) == downloads.get_workspace_dir()
    # exposed to the agent
    assert [d["path"] for d in svc._downloads] == [dl.saved_to]


def test_disallowed_download_is_quarantined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "locked")  # download denied
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS", "0")
    svc = BrowserService()
    dl = _FakeDownload("https://evil.test/x.zip", "x.zip")
    _run_save(svc, dl)
    assert dl.saved_to is not None
    qdir = downloads.get_quarantine_dir()
    assert os.path.commonpath([dl.saved_to, qdir]) == qdir
    # NOT exposed to the agent
    assert svc._downloads == []
