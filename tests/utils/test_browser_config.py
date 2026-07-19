"""Tests for ``tabvis.utils.browser_config`` — the browser-engine config layer.

This is where every engine decision is made (which binary, which profile, which stealth knobs), and
it is pure: no browser, no event loop. The launcher below it is a thin ``if engine == "cloak"``, so
covering this module covers the branching.

Deliberately SYNCHRONOUS — see tests/conftest.py for the ambient state these rely on being cleared.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from tabvis.utils import browser_config
from tabvis.utils.browser_config import (
    BROWSER_ENGINE_CATALOG,
    BrowserLaunchConfig,
    CloakLaunchConfig,
    camoufox_available,
    cloakbrowser_available,
    detect_browser_binary,
    engine_package_available,
    get_browser_cdp_endpoint,
    get_browser_channel,
    get_browser_connect_mode,
    get_browser_engine,
    get_browser_human_preset,
    get_browser_locale,
    get_browser_proxy,
    get_browser_timezone,
    get_browser_type,
    get_browser_user_data_dir,
    get_browser_ws_endpoint,
    get_cloak_browser_version,
    get_cloak_license_key,
    get_engine_spec,
    is_browser_auto_visual,
    is_browser_geoip,
    is_browser_humanize,
    is_browser_stealth,
    load_browser_launch_config,
    redact_proxy,
)
from tabvis.utils.config_constants import (
    BROWSER_CHANNELS,
    BROWSER_CONNECT_MODES,
    BROWSER_ENGINES,
    BROWSER_HUMAN_PRESETS,
    BROWSER_TYPES,
)
from tabvis.utils.settings.types import SettingsJson

# The two engines the pre-existing tests exercise; both remain valid catalog keys.
CHROMIUM = "chromium"
CLOAK = "cloak"


# --------------------------------------------------------------------------- engine selection


def test_engine_defaults_to_chromium() -> None:
    """No env, no settings => the stock, non-stealth engine."""
    assert get_browser_engine() == CHROMIUM
    assert is_browser_stealth() is False


def test_engine_env_selects_cloak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", CLOAK)
    assert get_browser_engine() == CLOAK
    assert is_browser_stealth() is True


def test_engine_from_settings_camel_case_alias(settings) -> None:
    """The settings field is reachable through its wire alias ``browserEngine``.

    The stub is built with ``SettingsJson.model_validate({...})``, so a typo'd alias would land in
    the model's ``extra`` (it is ``extra="allow"``) and this assertion would catch it.
    """
    settings(browserEngine=CLOAK)
    assert get_browser_engine() == CLOAK
    assert is_browser_stealth() is True


def test_env_beats_settings(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """Precedence: env > settings.json. An explicit env var wins even against a set field."""
    settings(browserEngine=CLOAK)
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", CHROMIUM)
    assert get_browser_engine() == CHROMIUM
    assert is_browser_stealth() is False


def test_unknown_engine_env_falls_back_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus value must degrade to the default, never blow up a session at launch."""
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "bogus")
    assert get_browser_engine() == CHROMIUM


def test_unknown_engine_setting_falls_back_without_raising(settings) -> None:
    settings(browserEngine="bogus")
    assert get_browser_engine() == CHROMIUM


# --------------------------------------------------------------------------- profile directory


def test_user_data_dir_differs_per_engine_by_default(
    monkeypatch: pytest.MonkeyPatch, config_home: str
) -> None:
    """Each engine gets its OWN profile: the two are different Chromium builds.

    A profile is migrated forward on open and is not safe to hand back to an older binary, so the
    default paths must not collide.
    """
    assert get_browser_user_data_dir() == os.path.join(config_home, "browser")

    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", CLOAK)
    cloak_dir = get_browser_user_data_dir()
    assert cloak_dir == os.path.join(config_home, "browser-cloak")
    assert cloak_dir != os.path.join(config_home, "browser")


def test_explicit_user_data_dir_env_overrides_both_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit dir is the user's deliberate call and wins for either engine."""
    monkeypatch.setenv("TABVIS_BROWSER_USER_DATA_DIR", "/tmp/shared-profile")

    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", CHROMIUM)
    assert get_browser_user_data_dir() == "/tmp/shared-profile"

    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", CLOAK)
    assert get_browser_user_data_dir() == "/tmp/shared-profile"


def test_explicit_user_data_dir_setting_overrides_both_engines(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    settings(browserUserDataDir="/tmp/from-settings")

    assert get_browser_user_data_dir() == "/tmp/from-settings"

    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", CLOAK)
    assert get_browser_user_data_dir() == "/tmp/from-settings"


# --------------------------------------------------------------------------- package availability


def test_cloakbrowser_available_reports_installed(cloak_package) -> None:
    cloak_package(True)
    assert cloakbrowser_available() is True


def test_cloakbrowser_available_reports_missing(cloak_package) -> None:
    """The absent case is what makes the launcher refuse rather than silently downgrade."""
    cloak_package(False)
    assert cloakbrowser_available() is False


def test_cloakbrowser_available_memoizes(cloak_package) -> None:
    """The answer is cached in a module global — hence conftest's reset fixture."""
    cloak_package(False)
    assert cloakbrowser_available() is False
    assert browser_config._CLOAKBROWSER_AVAILABLE is False

    cloak_package(True)  # resets the memo, so the new answer is picked up
    assert cloakbrowser_available() is True


# --------------------------------------------------------------------------- cloak knobs


def test_humanize_defaults_off_and_is_env_toggled(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    """Off by default: it paces input at roughly half a second per character."""
    assert is_browser_humanize() is False

    monkeypatch.setenv("TABVIS_BROWSER_HUMANIZE", "1")
    assert is_browser_humanize() is True

    monkeypatch.setenv("TABVIS_BROWSER_HUMANIZE", "0")
    assert is_browser_humanize() is False  # a defined-falsy env beats a truthy setting

    settings(browserHumanize=True)
    assert is_browser_humanize() is False
    monkeypatch.delenv("TABVIS_BROWSER_HUMANIZE")
    assert is_browser_humanize() is True


def test_human_preset_default_and_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_browser_human_preset() == "default"
    assert get_browser_human_preset() in BROWSER_HUMAN_PRESETS

    monkeypatch.setenv("TABVIS_BROWSER_HUMAN_PRESET", "careful")
    assert get_browser_human_preset() == "careful"


def test_human_preset_unknown_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_HUMAN_PRESET", "reckless")
    assert get_browser_human_preset() == "default"

    monkeypatch.delenv("TABVIS_BROWSER_HUMAN_PRESET")
    settings(browserHumanPreset="reckless")
    assert get_browser_human_preset() == "default"


def test_human_preset_from_settings_alias(settings) -> None:
    settings(browserHumanPreset="careful")
    assert get_browser_human_preset() == "careful"


def test_geoip_defaults_off(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    assert is_browser_geoip() is False

    settings(browserGeoip=True)
    assert is_browser_geoip() is True

    monkeypatch.setenv("TABVIS_BROWSER_GEOIP", "0")
    assert is_browser_geoip() is False


def test_proxy_timezone_locale_and_version_read_env_then_settings(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    assert get_browser_proxy() is None
    assert get_browser_timezone() is None
    assert get_browser_locale() is None
    assert get_cloak_browser_version() is None

    settings(
        browserProxy="http://from-settings:8080",
        browserTimezone="Europe/Berlin",
        browserLocale="de-DE",
        browserCloakVersion="145.0.1",
    )
    assert get_browser_proxy() == "http://from-settings:8080"
    assert get_browser_timezone() == "Europe/Berlin"
    assert get_browser_locale() == "de-DE"
    assert get_cloak_browser_version() == "145.0.1"

    monkeypatch.setenv("TABVIS_BROWSER_PROXY", "http://from-env:9090")
    monkeypatch.setenv("TABVIS_BROWSER_TIMEZONE", "America/New_York")
    monkeypatch.setenv("TABVIS_BROWSER_LOCALE", "en-US")
    monkeypatch.setenv("TABVIS_BROWSER_CLOAK_VERSION", "149.0.2")
    assert get_browser_proxy() == "http://from-env:9090"
    assert get_browser_timezone() == "America/New_York"
    assert get_browser_locale() == "en-US"
    assert get_cloak_browser_version() == "149.0.2"


# --------------------------------------------------------------------------- the license key


def test_license_key_defaults_to_none() -> None:
    """Unset => the free-tier binary, not an error."""
    assert get_cloak_license_key() is None


def test_license_key_from_tabvis_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_CLOAK_LICENSE_KEY", "tabvis-key")
    assert get_cloak_license_key() == "tabvis-key"


def test_license_key_falls_back_to_cloakbrowsers_own_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine already set up for CloakBrowser needs no tabvis-specific configuration."""
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", "native-key")
    assert get_cloak_license_key() == "native-key"


def test_license_key_prefers_the_tabvis_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_CLOAK_LICENSE_KEY", "tabvis-key")
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", "native-key")
    assert get_cloak_license_key() == "tabvis-key"


def test_license_key_has_no_settings_field(settings) -> None:
    """The key is a CREDENTIAL: env-only, deliberately absent from settings.json.

    settings.json is plain config that is read back and echoed; a paid key has no business in it. A
    field added there in future must fail this test.
    """
    aliases = {
        (f.alias or name) for name, f in SettingsJson.model_fields.items()
    } | set(SettingsJson.model_fields)
    assert not [a for a in aliases if "icense" in a]

    # Even if someone writes it into the file, extra="allow" parks it in `extra` and nothing reads it.
    settings(browserCloakLicenseKey="should-be-ignored")
    assert get_cloak_license_key() is None


# --------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://user:hunter2@proxy.example.com:8080", "http://***@proxy.example.com:8080"),
        ("socks5://user:hunter2@10.0.0.1:1080", "socks5://***@10.0.0.1:1080"),
        ("http://proxy.example.com:8080", "http://proxy.example.com:8080"),  # no creds, unchanged
        (None, None),
        ("", None),
    ],
)
def test_redact_proxy(url: str | None, expected: str | None) -> None:
    assert redact_proxy(url) == expected


def test_redact_proxy_never_leaks_the_password() -> None:
    """browser_info() is persisted to disk and served over the unauthenticated HTTP API."""
    redacted = redact_proxy("http://alice:hunter2@proxy.example.com:8080")
    assert "hunter2" not in redacted
    assert "alice" not in redacted
    assert "proxy.example.com:8080" in redacted  # the useful part survives


def test_cloak_config_redacted_never_leaks_the_license_key() -> None:
    cfg = CloakLaunchConfig(
        proxy="http://alice:hunter2@proxy.example.com:8080",
        humanize=True,
        human_preset="careful",
        license_key="super-secret-key",
    )
    view = cfg.redacted()

    assert view["licensed"] is True  # a boolean, never the key
    assert "super-secret-key" not in repr(view)
    assert "hunter2" not in repr(view)
    assert view["proxy"] == "http://***@proxy.example.com:8080"
    assert view["human_preset"] == "careful"


def test_cloak_config_redacted_without_a_license_or_humanize() -> None:
    view = CloakLaunchConfig().redacted()
    assert view["licensed"] is False
    assert view["proxy"] is None
    assert view["human_preset"] is None  # not meaningful when humanize is off


# --------------------------------------------------------------------------- launch snapshot


def test_load_browser_launch_config_defaults(config_home: str) -> None:
    cfg = load_browser_launch_config()
    assert isinstance(cfg, BrowserLaunchConfig)
    assert cfg.engine == CHROMIUM
    assert cfg.user_data_dir == os.path.join(config_home, "browser")
    assert cfg.cloak == CloakLaunchConfig()  # every stealth knob at its default


def test_load_browser_launch_config_populates_engine_and_every_cloak_field(
    monkeypatch: pytest.MonkeyPatch, config_home: str
) -> None:
    """One snapshot read at launch must wire up the engine and EVERY cloak knob.

    The field-by-field sweep at the end fails if a knob is added to CloakLaunchConfig but never
    wired into load_browser_launch_config() — which would silently ignore the user's setting.
    """
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", CLOAK)
    monkeypatch.setenv("TABVIS_BROWSER_PROXY", "http://alice:hunter2@proxy.example.com:8080")
    monkeypatch.setenv("TABVIS_BROWSER_HUMANIZE", "1")
    monkeypatch.setenv("TABVIS_BROWSER_HUMAN_PRESET", "careful")
    monkeypatch.setenv("TABVIS_BROWSER_GEOIP", "1")
    monkeypatch.setenv("TABVIS_BROWSER_TIMEZONE", "America/New_York")
    monkeypatch.setenv("TABVIS_BROWSER_LOCALE", "en-US")
    monkeypatch.setenv("TABVIS_BROWSER_CLOAK_LICENSE_KEY", "pro-key")
    monkeypatch.setenv("TABVIS_BROWSER_CLOAK_VERSION", "149.0.2")

    cfg = load_browser_launch_config()

    assert cfg.engine == CLOAK
    assert cfg.user_data_dir == os.path.join(config_home, "browser-cloak")

    assert cfg.cloak == CloakLaunchConfig(
        proxy="http://alice:hunter2@proxy.example.com:8080",
        humanize=True,
        human_preset="careful",
        geoip=True,
        timezone="America/New_York",
        locale="en-US",
        license_key="pro-key",
        browser_version="149.0.2",
    )

    defaults = CloakLaunchConfig()
    for f in dataclasses.fields(CloakLaunchConfig):
        assert getattr(cfg.cloak, f.name) != getattr(defaults, f.name), (
            f"CloakLaunchConfig.{f.name} was not populated by load_browser_launch_config()"
        )


# --------------------------------------------------------------------------- the engine catalog
#
# The compatibility matrix (config_constants.BROWSER_ENGINES + BROWSER_ENGINE_CATALOG) and the
# accessors that read it. Still pure — no browser, no event loop.


def test_catalog_keys_match_browser_engines_constant() -> None:
    """The dependency-free constant and the rich catalog must stay in lockstep.

    ``get_browser_engine`` validates against BROWSER_ENGINES; the launcher reads the catalog. If a
    key exists in one but not the other, an engine is either unselectable or unlaunchable.
    """
    assert set(BROWSER_ENGINE_CATALOG) == set(BROWSER_ENGINES)
    # ...and every spec's own key field agrees with its dict key.
    for key, spec in BROWSER_ENGINE_CATALOG.items():
        assert spec.key == key


def test_every_spec_is_internally_consistent() -> None:
    """Each catalog entry names a real driver, a real mode, and valid channels."""
    for spec in BROWSER_ENGINE_CATALOG.values():
        assert spec.browser_type in BROWSER_TYPES, spec.key
        assert spec.mode in BROWSER_CONNECT_MODES, spec.key
        assert spec.kernel in BROWSER_TYPES, spec.key
        if spec.channel is not None:
            assert spec.channel in BROWSER_CHANNELS, spec.key
        # Plugin engines name their SDK and a required package; cdp/connect are 'remote'.
        if spec.mode == "plugin":
            assert spec.requires is not None, spec.key
        assert spec.remote == (spec.mode in ("cdp", "connect")), spec.key


@pytest.mark.parametrize(
    ("engine", "browser_type", "mode", "stealth"),
    [
        ("chromium", "chromium", "launch", False),
        ("chrome", "chromium", "launch", False),
        ("msedge", "chromium", "launch", False),
        ("brave", "chromium", "launch", False),
        ("firefox", "firefox", "launch", False),
        ("webkit", "webkit", "launch", False),
        ("cloak", "chromium", "plugin", True),
        ("camoufox", "firefox", "plugin", True),
        ("cdp", "chromium", "cdp", False),
        ("steel", "chromium", "cdp", False),
        ("adspower", "chromium", "cdp", False),
        ("connect", "chromium", "connect", False),
        ("browserbase", "chromium", "connect", False),
    ],
)
def test_engine_resolves_type_mode_and_stealth(
    monkeypatch: pytest.MonkeyPatch, engine: str, browser_type: str, mode: str, stealth: bool
) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", engine)
    assert get_browser_engine() == engine
    assert get_browser_type() == browser_type
    assert get_browser_connect_mode() == mode
    assert is_browser_stealth() is stealth


def test_get_engine_spec_unknown_falls_back_to_chromium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown name must not raise — same forgiving contract as get_browser_engine."""
    assert get_engine_spec("bogus").key == "chromium"
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "bogus")
    assert get_engine_spec().key == "chromium"  # reads the (already-defaulted) configured engine


# --------------------------------------------------------------------------- channels

def test_msedge_is_a_valid_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_CHANNEL", "msedge")
    assert get_browser_channel() == "msedge"


def test_engine_supplies_its_intrinsic_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chrome/msedge engines carry their channel; an explicit override still wins."""
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "chrome")
    assert get_browser_channel() == "chrome"

    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "msedge")
    assert get_browser_channel() == "msedge"

    monkeypatch.setenv("TABVIS_BROWSER_CHANNEL", "chromium")  # explicit override beats the engine
    assert get_browser_channel() == "chromium"


def test_plain_chromium_engine_has_no_intrinsic_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "chromium")
    assert get_browser_channel() == "chromium"  # the 'bundled build' sentinel


# --------------------------------------------------------------------------- profile dirs per engine

def test_profile_dir_is_per_engine(
    monkeypatch: pytest.MonkeyPatch, config_home: str
) -> None:
    """Different binaries/kernels must not share a profile dir (see get_browser_user_data_dir)."""
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "chromium")
    assert get_browser_user_data_dir() == os.path.join(config_home, "browser")

    for engine, suffix in [
        ("firefox", "browser-firefox"),
        ("webkit", "browser-webkit"),
        ("chrome", "browser-chrome"),
        ("camoufox", "browser-camoufox"),
    ]:
        monkeypatch.setenv("TABVIS_BROWSER_ENGINE", engine)
        assert get_browser_user_data_dir() == os.path.join(config_home, suffix), engine


# --------------------------------------------------------------------------- remote-attach endpoints

def test_cdp_endpoint_reads_env_then_settings(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    assert get_browser_cdp_endpoint() is None
    settings(browserCdpEndpoint="http://from-settings:9222")
    assert get_browser_cdp_endpoint() == "http://from-settings:9222"
    monkeypatch.setenv("TABVIS_BROWSER_CDP_ENDPOINT", "http://from-env:9333")
    assert get_browser_cdp_endpoint() == "http://from-env:9333"


def test_ws_endpoint_reads_env_then_settings(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    assert get_browser_ws_endpoint() is None
    settings(browserWsEndpoint="wss://from-settings/x")
    assert get_browser_ws_endpoint() == "wss://from-settings/x"
    monkeypatch.setenv("TABVIS_BROWSER_WS_ENDPOINT", "wss://from-env/y")
    assert get_browser_ws_endpoint() == "wss://from-env/y"


# --------------------------------------------------------------------------- binary auto-detection

def test_detect_browser_binary_absolute_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An existing absolute candidate is returned; a missing name yields None."""
    fake = tmp_path / "Brave Browser"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(browser_config.sys, "platform", "linux")
    monkeypatch.setitem(
        browser_config._BROWSER_BINARY_CANDIDATES, "brave", {"linux": (str(fake),)}
    )
    assert detect_browser_binary("brave") == str(fake)
    assert detect_browser_binary(None) is None
    assert detect_browser_binary("no-such-engine") is None


def test_detect_browser_binary_resolves_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare command candidate is resolved via shutil.which."""
    monkeypatch.setattr(browser_config.sys, "platform", "linux")
    monkeypatch.setitem(
        browser_config._BROWSER_BINARY_CANDIDATES, "vivaldi", {"linux": ("vivaldi-xyz",)}
    )
    monkeypatch.setattr(browser_config.shutil, "which", lambda c: "/usr/bin/" + c)
    assert detect_browser_binary("vivaldi") == "/usr/bin/vivaldi-xyz"


def test_executable_path_explicit_beats_autodetect(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.utils.browser_config import get_browser_executable_path

    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "brave")
    monkeypatch.setenv("TABVIS_BROWSER_EXECUTABLE_PATH", "/opt/custom/brave")
    assert get_browser_executable_path() == "/opt/custom/brave"


# --------------------------------------------------------------------------- camoufox availability

def test_camoufox_available_reports_installed(camoufox_package) -> None:
    camoufox_package(True)
    assert camoufox_available() is True


def test_camoufox_available_reports_missing(camoufox_package) -> None:
    camoufox_package(False)
    assert camoufox_available() is False


def test_engine_package_available_routes_to_the_right_check(
    cloak_package, camoufox_package
) -> None:
    assert engine_package_available(None) is True  # nothing required
    cloak_package(False)
    assert engine_package_available("cloakbrowser") is False
    camoufox_package(True)
    assert engine_package_available("camoufox") is True


# --------------------------------------------------------------------------- launch snapshot (new fields)

def test_launch_config_carries_type_mode_and_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cdp engine freezes its browser_type/mode and the endpoint onto the launch snapshot."""
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "adspower")
    monkeypatch.setenv("TABVIS_BROWSER_CDP_ENDPOINT", "http://127.0.0.1:9222")
    cfg = load_browser_launch_config()
    assert cfg.engine == "adspower"
    assert cfg.browser_type == "chromium"
    assert cfg.mode == "cdp"
    assert cfg.cdp_endpoint == "http://127.0.0.1:9222"
    assert cfg.stealth is False


def test_launch_config_firefox_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ENGINE", "firefox")
    cfg = load_browser_launch_config()
    assert cfg.browser_type == "firefox"
    assert cfg.kernel == "firefox"
    assert cfg.mode == "launch"


# --------------------------------------------------------------------------- auto-visual supplement


def test_auto_visual_defaults_on_and_is_env_toggled(monkeypatch: pytest.MonkeyPatch) -> None:
    """On by default (sparse aria => screenshot + HTML); a defined-falsy env forces text-only."""
    assert is_browser_auto_visual() is True

    monkeypatch.setenv("TABVIS_BROWSER_AUTO_VISUAL", "0")
    assert is_browser_auto_visual() is False

    monkeypatch.setenv("TABVIS_BROWSER_AUTO_VISUAL", "1")
    assert is_browser_auto_visual() is True
