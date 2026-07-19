"""PP-7 — filesystem adapter: path classification + config-write protection.

classify_path resolves symlinks before classifying, so a link cannot smuggle a write out of the
workspace or into the config area. The adapter preserves ordinary read/write but hard-denies writes to
config:/sensitive paths in every mode. No real tool call is executed — the adapter is driven directly.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.policy import grants
from tabvis.policy.filesystem_adapter import evaluate_path
from tabvis.policy.fs_resource import classify_path
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("TABVIS_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("TABVIS_PERMISSION_SHADOW", raising=False)
    grants.clear()
    reset_settings_cache()
    yield
    grants.clear()
    reset_settings_cache()


def _ctx() -> Any:
    return SimpleNamespace(agent_id="agF", tool_use_id="tu_1")


# --------------------------------------------------------------------------- classification


def test_workspace_path(tmp_path) -> None:
    cwd = str(tmp_path)
    r = classify_path("notes/a.md", cwd=cwd, config_home=str(tmp_path / ".cfg"))
    assert r == "workspace:notes/a.md"


def test_config_home_path(tmp_path) -> None:
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    r = classify_path(str(cfg / "settings.json"), cwd=str(tmp_path / "proj"), config_home=str(cfg))
    assert r.startswith("config:")


def test_sensitive_dotenv_in_workspace_is_secret(tmp_path) -> None:
    cwd = str(tmp_path)
    r = classify_path(".env", cwd=cwd, config_home=str(tmp_path / ".cfg"))
    assert r.startswith("secret:")  # sensitive secret even though it lives in the workspace


def test_out_of_tree_is_fs(tmp_path) -> None:
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    r = classify_path(str(tmp_path / "elsewhere.txt"), cwd=cwd, config_home=str(tmp_path / ".cfg"))
    assert r.startswith("fs:")


def test_symlink_escape_out_of_workspace(tmp_path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    link = cwd / "link.txt"
    link.symlink_to(outside)
    r = classify_path(str(link), cwd=str(cwd), config_home=str(tmp_path / ".cfg"))
    # realpath resolves the link to the outside target → not workspace:
    assert r.startswith("fs:") and "proj" not in r.split(":", 1)[1]


def test_symlink_into_config_classifies_config(tmp_path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    (cfg / "secret.txt").write_text("s")
    link = cwd / "sneaky.txt"
    link.symlink_to(cfg / "secret.txt")
    r = classify_path(str(link), cwd=str(cwd), config_home=str(cfg))
    assert r.startswith("config:")  # link target is under config → protected


# --------------------------------------------------------------------------- adapter enforcement


def _patch_roots(monkeypatch: pytest.MonkeyPatch, cwd: str, cfg: str) -> None:
    import tabvis.policy.fs_resource as fsr

    monkeypatch.setattr(fsr, "classify_path", fsr.classify_path)  # keep real
    monkeypatch.setattr("tabvis.bootstrap.state.get_original_cwd", lambda: cwd)
    monkeypatch.setattr("tabvis.utils.env_utils.get_tabvis_config_home_dir", lambda: cfg)


def test_workspace_write_allowed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roots(monkeypatch, str(tmp_path), str(tmp_path / ".cfg"))
    d = evaluate_path("filesystem.write", "notes/a.md", _ctx(), {"file_path": "notes/a.md"})
    assert d["behavior"] == "allow"


def test_config_write_denied_in_all_modes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    _patch_roots(monkeypatch, str(tmp_path / "proj"), str(cfg))
    target = str(cfg / "settings.json")
    for mode in ("trusted", "standard", "locked"):
        monkeypatch.setenv("TABVIS_PERMISSION_MODE", mode)
        d = evaluate_path("filesystem.write", target, _ctx(), {"file_path": target})
        assert d["behavior"] == "deny", mode
        # standard's baseline std-protect-config matches first; trusted/locked hit fs-protect-config.
        assert d["decisionReason"]["rule"].endswith("protect-config"), mode


def test_config_read_still_allowed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    _patch_roots(monkeypatch, str(tmp_path / "proj"), str(cfg))
    target = str(cfg / "settings.json")
    d = evaluate_path("filesystem.read", target, _ctx(), {"file_path": target})
    assert d["behavior"] == "allow"


def test_dotenv_write_denied(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roots(monkeypatch, str(tmp_path), str(tmp_path / ".cfg"))
    d = evaluate_path("filesystem.write", ".env", _ctx(), {"file_path": ".env"})
    assert d["behavior"] == "deny"


def test_shadow_serves_config_write_as_allow(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".cfg"
    cfg.mkdir()
    _patch_roots(monkeypatch, str(tmp_path / "proj"), str(cfg))
    monkeypatch.setenv("TABVIS_PERMISSION_SHADOW", "1")
    target = str(cfg / "settings.json")
    d = evaluate_path("filesystem.write", target, _ctx(), {"file_path": target})
    assert d["behavior"] == "allow" and d["decisionReason"]["wouldBe"] == "deny"
