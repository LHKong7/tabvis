"""PP-2 — load + compile policy rules from settings.json (docs/permission-policy-engine_v1.md §8).

Exercises the impure edge that bridges the settings module to the pure policy core: reading
``permissions.rules`` / ``permissions.mode``, compiling with fail-loud validation, and layering the
settings rules over the mode baseline. Settings are passed explicitly (no disk / no
``get_initial_settings``) so the tests stay hermetic.
"""

from __future__ import annotations

import pytest

from tabvis.policy import (
    PolicyConfigError,
    build_policy_engine,
    load_policy_rules_from_settings,
    read_mode_from_settings,
)
from tabvis.utils.settings.types import SettingsJson


def _settings(permissions: dict | None) -> SettingsJson:
    """Build a real SettingsJson via model_validate so field/extra passthrough is exercised."""
    return SettingsJson.model_validate({"permissions": permissions} if permissions is not None else {})


# --------------------------------------------------------------------------- rule loading


def test_rules_field_parses_and_compiles() -> None:
    s = _settings(
        {
            "rules": [
                {"id": "r1", "effect": "allow", "actions": ["filesystem.write"], "resources": ["workspace:**"]},
                {"id": "r2", "effect": "deny", "actions": ["network.request"], "resources": ["url:https://**"]},
            ]
        }
    )
    rules = load_policy_rules_from_settings(s)
    assert [r.id for r in rules] == ["r1", "r2"]
    assert rules[1].effect == "deny"


def test_legacy_allow_deny_ask_untouched_and_no_policy_rules() -> None:
    # A config with only the legacy string lists yields zero policy rules (no crash).
    s = _settings({"allow": ["Bash(ls)"], "deny": ["Bash(rm)"]})
    assert load_policy_rules_from_settings(s) == []


def test_missing_permissions_returns_empty() -> None:
    assert load_policy_rules_from_settings(_settings(None)) == []


def test_invalid_rule_raises_with_settings_context() -> None:
    s = _settings({"rules": [{"id": "bad", "effect": "maybe", "actions": ["filesystem.read"], "resources": ["workspace:**"]}]})
    with pytest.raises(PolicyConfigError, match="settings.json permissions.rules"):
        load_policy_rules_from_settings(s)


def test_rules_not_a_list_raises() -> None:
    # The typed SettingsJson field rejects a non-list at parse time (pydantic). The loader keeps its
    # own guard for non-pydantic callers — exercise that path with a raw settings-shaped object.
    from types import SimpleNamespace

    raw = SimpleNamespace(permissions=SimpleNamespace(rules={"id": "x"}, model_extra=None))
    with pytest.raises(PolicyConfigError, match="must be a list"):
        load_policy_rules_from_settings(raw)


def test_duplicate_ids_across_settings_rules_raise() -> None:
    s = _settings(
        {
            "rules": [
                {"id": "dup", "effect": "allow", "actions": ["filesystem.read"], "resources": ["workspace:**"]},
                {"id": "dup", "effect": "deny", "actions": ["filesystem.read"], "resources": ["workspace:**"]},
            ]
        }
    )
    with pytest.raises(PolicyConfigError):
        load_policy_rules_from_settings(s)


# --------------------------------------------------------------------------- mode reading


def test_mode_defaults_to_standard() -> None:
    assert read_mode_from_settings(_settings(None)) == "standard"
    assert read_mode_from_settings(_settings({"allow": []})) == "standard"


def test_mode_read_from_settings() -> None:
    assert read_mode_from_settings(_settings({"mode": "locked"})) == "locked"


def test_invalid_mode_raises() -> None:
    with pytest.raises(PolicyConfigError, match="permissions.mode"):
        read_mode_from_settings(_settings({"mode": "paranoid"}))


# --------------------------------------------------------------------------- engine assembly


def test_build_engine_layers_settings_over_baseline() -> None:
    # A settings allow upgrades the standard baseline's ask for an external download.
    s = _settings(
        {
            "mode": "standard",
            "rules": [
                {"id": "grant-ok", "effect": "allow", "actions": ["browser.download"], "resources": ["url:https://ok.test/**"]},
            ],
        }
    )
    eng = build_policy_engine(settings=s)
    assert eng.evaluate("browser.download", "url:https://ok.test/f").effect == "allow"
    assert eng.evaluate("browser.download", "url:https://other.test/f").effect == "ask"  # baseline


def test_build_engine_settings_deny_is_absolute() -> None:
    s = _settings(
        {
            "mode": "standard",
            "rules": [
                {"id": "block-host", "effect": "deny", "actions": ["network.request"], "resources": ["url:https://evil.test/**"]},
            ],
        }
    )
    eng = build_policy_engine(settings=s)
    assert eng.evaluate("network.request", "url:https://evil.test/x").effect == "deny"


def test_build_engine_explicit_mode_overrides_settings_mode() -> None:
    s = _settings({"mode": "standard"})
    eng = build_policy_engine(mode="locked", settings=s)
    assert eng.evaluate("filesystem.write", "workspace:a").effect == "deny"  # locked fallback


def test_build_engine_extra_rules_outrank_settings() -> None:
    # extra_rules (identity/grant) layer above settings rules.
    from tabvis.policy import compile_rules

    s = _settings(
        {"rules": [{"id": "s-ask", "effect": "ask", "actions": ["browser.upload"], "resources": ["**"]}]}
    )
    grant = compile_rules(
        [{"id": "g-allow", "effect": "allow", "actions": ["browser.upload"], "resources": ["url:https://ok.test/**"]}]
    )
    eng = build_policy_engine(settings=s, extra_rules=grant)
    assert eng.evaluate("browser.upload", "url:https://ok.test/f").effect == "allow"
