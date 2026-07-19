"""PP-4 — permission mode resolution (env > settings > default) + first-class switch.

``docs/permission-policy-engine_v1.md`` §4.4 / §8 PP-4. The TABVIS_PERMISSION_MODE env var is the
one-off switch; when unset the settings-file value applies, else ``standard``. The resolved mode
drives the same engine everywhere, so behavior is consistent across whatever surface invokes a tool.
The headless ask→deny posture is enforced downstream and is unaffected by the mode.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.browser import identity_store, policy_guard
from tabvis.policy import (
    PolicyConfigError,
    build_policy_engine,
    resolve_mode,
)
from tabvis.utils.settings.settings import reset_settings_cache
from tabvis.utils.settings.types import SettingsJson


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("TABVIS_PERMISSION_MODE", raising=False)
    identity_store._cache.clear()
    reset_settings_cache()
    yield
    identity_store._cache.clear()
    reset_settings_cache()


def _settings(mode: str | None) -> SettingsJson:
    perms = {"mode": mode} if mode is not None else {}
    return SettingsJson.model_validate({"permissions": perms})


# --------------------------------------------------------------------------- resolution priority


def test_default_is_standard() -> None:
    assert resolve_mode(_settings(None)) == "standard"


def test_settings_used_when_no_env() -> None:
    assert resolve_mode(_settings("locked")) == "locked"


def test_env_overrides_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "trusted")
    assert resolve_mode(_settings("locked")) == "trusted"


def test_env_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "LOCKED")
    assert resolve_mode(_settings(None)) == "locked"


def test_blank_env_falls_through_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "  ")
    assert resolve_mode(_settings("trusted")) == "trusted"


def test_invalid_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "paranoid")
    with pytest.raises(PolicyConfigError, match="TABVIS_PERMISSION_MODE"):
        resolve_mode(_settings(None))


def test_build_engine_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "trusted")
    eng = build_policy_engine(settings=_settings("standard"))
    # trusted → fallback allow, so an external download is allowed rather than asked.
    assert eng.evaluate("browser.download", "url:https://x.test/f").effect == "allow"


# --------------------------------------------------------------------------- end-to-end via adapter


def test_adapter_locked_mode_denies_future_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "locked")
    eng = policy_guard._browser_engine(None)
    # navigation/interaction still explicitly allowed by the browser baseline...
    assert eng.evaluate("browser.navigate", "url:https://x.test/").effect == "allow"
    assert eng.evaluate("browser.interact", "session:page").effect == "allow"
    # ...but a download falls to the locked fallback (deny), not ask.
    assert eng.evaluate("browser.download", "url:https://x.test/f").effect == "deny"


def test_adapter_trusted_mode_allows_future_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "trusted")
    eng = policy_guard._browser_engine(None)
    assert eng.evaluate("browser.download", "url:https://x.test/f").effect == "allow"


def test_adapter_navigation_allow_preserved_across_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plain goto with no allowlist stays allow in every mode (browser baseline is explicit).
    for mode in ("trusted", "standard", "locked"):
        monkeypatch.setenv("TABVIS_PERMISSION_MODE", mode)
        d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://a.com"}, SimpleNamespace(agent_id=None))
        assert d["behavior"] == "allow", mode
