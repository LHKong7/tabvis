"""Download workspace: location resolution, filename safety, and collision-free naming."""

from __future__ import annotations

import os

from tabvis.browser import downloads


def test_workspace_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TABVIS_WORKSPACE_DIR", str(tmp_path / "ws"))
    assert downloads.get_workspace_dir() == os.path.abspath(str(tmp_path / "ws"))


def test_workspace_default_is_per_session(monkeypatch) -> None:
    monkeypatch.delenv("TABVIS_WORKSPACE_DIR", raising=False)
    wd = downloads.get_workspace_dir()
    assert wd.endswith(os.path.join("workspace"))
    assert "projects" in wd  # <config-home>/projects/<cwd>/<session-id>/workspace


def test_filename_from_url() -> None:
    assert downloads.filename_from_url("https://x.com/a/report.pdf") == "report.pdf"
    assert downloads.filename_from_url("https://x.com/a/my%20file.csv") == "my file.csv"
    assert downloads.filename_from_url("https://x.com/") == "download"
    assert downloads.filename_from_url(None) == "download"


def test_safe_name_strips_paths_and_unsafe(monkeypatch) -> None:
    # a hostile suggested filename can't escape the dir
    assert downloads._safe_name("../../etc/passwd") == "passwd"
    assert downloads._safe_name("a/b/c.pdf") == "c.pdf"
    assert downloads._safe_name("weird:*?name.pdf").endswith(".pdf")
    assert downloads._safe_name("") == "download"


def test_unique_path_never_clobbers(tmp_path) -> None:
    d = str(tmp_path)
    p1 = downloads.unique_path(d, "report.pdf")
    assert os.path.basename(p1) == "report.pdf"
    open(p1, "w").close()
    p2 = downloads.unique_path(d, "report.pdf")
    assert os.path.basename(p2) == "report (2).pdf"
    open(p2, "w").close()
    p3 = downloads.unique_path(d, "report.pdf")
    assert os.path.basename(p3) == "report (3).pdf"
    # different name is unaffected
    assert os.path.basename(downloads.unique_path(d, "data.csv")) == "data.csv"
