"""PP-7 hardening — TOCTOU write re-check, strict FS mode, and directory grants.

Builds on the filesystem adapter: enforce_write re-classifies at the write point (catching a symlink
swapped in after check_permissions); strict mode makes out-of-tree writes need a grant and denies
secret reads; grant_directory opens a subtree. No real tool call is executed.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.policy import grants
from tabvis.policy.filesystem_adapter import (
    PolicyDenied,
    enforce_write,
    evaluate_path,
    grant_directory,
    is_fs_strict,
)
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    for var in ("TABVIS_PERMISSION_MODE", "TABVIS_PERMISSION_SHADOW", "TABVIS_PERMISSION_FS_STRICT"):
        monkeypatch.delenv(var, raising=False)
    grants.clear()
    reset_settings_cache()
    yield
    grants.clear()
    reset_settings_cache()


def _ctx(agent_id: str = "agH") -> Any:
    return SimpleNamespace(agent_id=agent_id, tool_use_id="tu_1")


def _roots(monkeypatch: pytest.MonkeyPatch, cwd: str, cfg: str) -> None:
    monkeypatch.setattr("tabvis.bootstrap.state.get_original_cwd", lambda: cwd)
    monkeypatch.setattr("tabvis.utils.env_utils.get_tabvis_config_home_dir", lambda: cfg)


# --------------------------------------------------------------------------- TOCTOU write re-check


def test_enforce_write_allows_workspace(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _roots(monkeypatch, str(tmp_path), str(tmp_path / ".cfg"))
    (tmp_path / "a.txt").write_text("x")
    enforce_write(str(tmp_path / "a.txt"), _ctx())  # no raise


def test_enforce_write_blocks_symlink_into_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text("{}")
    _roots(monkeypatch, str(cwd), str(cfg))
    # A path that passed check_permissions as a workspace file is now a symlink into config.
    link = cwd / "innocent.txt"
    link.symlink_to(cfg / "settings.json")
    with pytest.raises(PolicyDenied):
        enforce_write(str(link), _ctx())


def test_enforce_write_shadow_never_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    _roots(monkeypatch, str(tmp_path / "proj"), str(cfg))
    monkeypatch.setenv("TABVIS_PERMISSION_SHADOW", "1")
    enforce_write(str(cfg / "settings.json"), _ctx())  # would deny, but shadow → no raise


# --------------------------------------------------------------------------- strict mode


def test_strict_flag_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_fs_strict() is False
    monkeypatch.setenv("TABVIS_PERMISSION_FS_STRICT", "1")
    assert is_fs_strict() is True


def test_strict_denies_secret_read_lenient_allows(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _roots(monkeypatch, str(tmp_path), str(tmp_path / ".cfg"))
    env_path = str(tmp_path / ".env")
    # lenient: secret read allowed
    assert evaluate_path("filesystem.read", env_path, _ctx(), {"file_path": env_path})["behavior"] == "allow"
    # strict: secret read denied
    monkeypatch.setenv("TABVIS_PERMISSION_FS_STRICT", "1")
    d = evaluate_path("filesystem.read", env_path, _ctx(), {"file_path": env_path})
    assert d["behavior"] == "deny" and d["decisionReason"]["rule"] == "fs-protect-secret-read"


def test_strict_out_of_tree_write_asks_then_grant_allows(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    outside = tmp_path / "ext"
    outside.mkdir()
    _roots(monkeypatch, str(cwd), str(tmp_path / ".cfg"))
    monkeypatch.setenv("TABVIS_PERMISSION_FS_STRICT", "1")
    target = str(outside / "f.txt")
    # standard fallback for an unmatched fs: write is ask
    assert evaluate_path("filesystem.write", target, _ctx(), {"file_path": target})["behavior"] == "ask"
    # open the directory → subsequent write allowed
    grant_directory(str(outside), _ctx())
    assert evaluate_path("filesystem.write", target, _ctx(), {"file_path": target})["behavior"] == "allow"


def test_grant_directory_refuses_protected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    _roots(monkeypatch, str(tmp_path / "proj"), str(cfg))
    with pytest.raises(PolicyDenied):
        grant_directory(str(cfg), _ctx())


def test_lenient_out_of_tree_write_still_allowed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _roots(monkeypatch, str(cwd), str(tmp_path / ".cfg"))
    target = str(tmp_path / "ext.txt")  # out of tree, lenient mode
    assert evaluate_path("filesystem.write", target, _ctx(), {"file_path": target})["behavior"] == "allow"
