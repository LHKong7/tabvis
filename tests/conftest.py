"""Shared fixtures — isolating tabvis's config layer from the developer's real machine.

Every accessor under test reads from three places, in order: the process environment, the
session-cached ``settings.json``, and a hardcoded default. All three are ambient state on a real
checkout, so the fixtures here neutralise them:

* ``_clean_browser_env``  — the repo's ``.env`` is autoloaded by the tabvis entrypoint and the
  developer's shell may export ``TABVIS_BROWSER_*`` anyway (this repo's .env sets
  ``TABVIS_BROWSER_HEADLESS=0``). Every browser key is deleted so a test that does not set one is
  really testing the default.
* ``_config_home``        — pins ``TABVIS_CONFIG_DIR`` at a tmp dir, so profile-path assertions are
  exact and nothing under the developer's ``~/.tabvis`` is read or written.
* ``settings``            — replaces the settings source. NOTE it patches
  ``tabvis.utils.browser_config.get_initial_settings``, *not* the function on
  ``tabvis.utils.settings.settings``: ``browser_config`` does a ``from ... import get_initial_settings``
  at module import, so it holds its own reference and patching the origin module has no effect.
* ``reset_cloak_memo``/``cloak_package`` — ``cloakbrowser_available()`` memoizes into a module
  global, which would otherwise leak a faked answer into every later test.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from importlib.machinery import ModuleSpec

import pytest

from tabvis.utils import browser_config
from tabvis.utils.settings.types import SettingsJson

# Env keys that would otherwise leak the developer's environment into the assertions. Every
# TABVIS_BROWSER_* key is cleared by prefix (so a new knob is covered automatically); the license key
# also has a CloakBrowser-native alias that lives outside the prefix.
_BROWSER_ENV_PREFIX = "TABVIS_BROWSER"
_EXTRA_ENV_KEYS = ("CLOAKBROWSER_LICENSE_KEY",)


@pytest.fixture(autouse=True)
def _clean_browser_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every browser-related env var so the suite is deterministic."""
    for key in list(os.environ):
        if key.startswith(_BROWSER_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    for key in _EXTRA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def config_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """Point the tabvis config home at a tmp dir (the default profile path is derived from it)."""
    home = str(tmp_path / "tabvis-config")
    monkeypatch.setenv("TABVIS_CONFIG_DIR", home)
    return home


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Callable[..., SettingsJson]:
    """Install a stub ``settings.json``; autouse-installs an EMPTY one so disk never leaks in.

    Call it with **camelCase wire keys** (``settings(browserEngine="cloak")``) — the stub is built
    through ``SettingsJson.model_validate``, exactly like a real file, so a mistyped alias in
    ``types.py`` fails the test instead of silently landing in the model's ``extra``.
    """

    def _install(**wire_fields: object) -> SettingsJson:
        stub = SettingsJson.model_validate(dict(wire_fields))
        monkeypatch.setattr(browser_config, "get_initial_settings", lambda: stub)
        return stub

    _install()
    return _install


@pytest.fixture(autouse=True)
def reset_cloak_memo() -> None:
    """Clear the ``cloakbrowser_available()``/``camoufox_available()`` memos around each test."""
    browser_config._CLOAKBROWSER_AVAILABLE = None
    browser_config._CAMOUFOX_AVAILABLE = None
    yield
    browser_config._CLOAKBROWSER_AVAILABLE = None
    browser_config._CAMOUFOX_AVAILABLE = None


@pytest.fixture
def cloak_package(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Fake the presence/absence of the optional ``cloakbrowser`` package.

    It really is installed in this checkout (``uv sync --extra cloak``), so the absent case cannot be
    tested by importing; ``find_spec`` is stubbed for that one name and delegates for every other.
    """

    real_find_spec = importlib.util.find_spec

    def _set(present: bool) -> None:
        def fake_find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
            if name == "cloakbrowser":
                return ModuleSpec("cloakbrowser", loader=None) if present else None
            return real_find_spec(name, package)

        monkeypatch.setattr(browser_config.importlib.util, "find_spec", fake_find_spec)
        browser_config._CLOAKBROWSER_AVAILABLE = None  # drop anything memoized before the fake

    return _set


@pytest.fixture
def camoufox_package(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Fake the presence/absence of the optional ``camoufox`` package (see ``cloak_package``)."""

    real_find_spec = importlib.util.find_spec

    def _set(present: bool) -> None:
        def fake_find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
            if name == "camoufox":
                return ModuleSpec("camoufox", loader=None) if present else None
            return real_find_spec(name, package)

        monkeypatch.setattr(browser_config.importlib.util, "find_spec", fake_find_spec)
        browser_config._CAMOUFOX_AVAILABLE = None

    return _set
