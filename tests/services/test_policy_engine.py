"""PP-1 — permission policy engine core (docs/permission-policy-engine_v1.md).

Covers the pure core: action matching, resource normalization (traversal / case / IDN / glob), rule
compilation + validation, deny>ask>allow>fallback precedence, and the three mode baselines. Nothing
here launches a browser or touches the filesystem — the engine is a pure function of its ruleset.
"""

from __future__ import annotations

import pytest

from tabvis.policy import (
    PolicyConfigError,
    PolicyEngine,
    action_matches,
    compile_rule,
    compile_rules,
    normalize_resource,
    resource_matches,
)

# --------------------------------------------------------------------------- action matching


def test_action_exact_and_prefix_and_glob() -> None:
    assert action_matches("filesystem.write", "filesystem.write")
    assert action_matches("filesystem", "filesystem.write")  # category prefix
    assert action_matches("filesystem.*", "filesystem.write")
    assert action_matches("*", "anything.at.all")
    assert action_matches("**", "browser.download")
    assert not action_matches("filesystem.*", "filesystem")  # * needs a segment
    assert not action_matches("filesystem.read", "filesystem.write")
    assert not action_matches("network", "filesystem.write")


# --------------------------------------------------------------------------- resource normalization


def test_resource_namespace_split() -> None:
    r = normalize_resource("workspace:notes/a.md")
    assert r.namespace == "workspace" and r.path == "notes/a.md" and not r.escaped
    bare = normalize_resource("**")
    assert bare.namespace == "*" and bare.path == "**"


def test_resource_traversal_marks_escaped() -> None:
    assert normalize_resource("workspace:../etc/passwd").escaped
    assert normalize_resource("workspace:a/../../b").escaped
    assert not normalize_resource("workspace:a/../b").escaped  # stays within root


def test_resource_url_case_and_idn() -> None:
    a = normalize_resource("url:HTTPS://ExAmPle.COM/Path")
    assert a.path == "https://example.com/Path"  # scheme+host lowered, path preserved
    idn = normalize_resource("url:https://bücher.example/x")
    assert "xn--" in idn.path  # host IDN-encoded


def test_resource_glob_matching() -> None:
    ws = normalize_resource("workspace:a/b/c.txt")
    assert resource_matches("workspace:**", ws)
    assert resource_matches("workspace:a/**", ws)
    assert not resource_matches("workspace:a/*", ws)  # * is single-segment
    assert resource_matches("**", ws)  # wildcard namespace catches all
    # namespace must match
    assert not resource_matches("session:**", ws)


def test_escaped_resource_matches_no_namespaced_pattern() -> None:
    escaped = normalize_resource("workspace:../secrets")
    assert not resource_matches("workspace:**", escaped)


def test_url_pattern_wildcard_authority() -> None:
    concrete = normalize_resource("url:https://evil.test/x")
    assert resource_matches("url:https://**", concrete)
    assert not resource_matches("url:https://good.test/**", concrete)


# --------------------------------------------------------------------------- rule compilation


def test_compile_rule_valid() -> None:
    rule = compile_rule(
        {"id": "r1", "effect": "allow", "actions": ["filesystem.write"], "resources": ["workspace:**"]}
    )
    assert rule.id == "r1" and rule.effect == "allow"


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "", "effect": "allow", "actions": ["filesystem.read"], "resources": ["workspace:**"]},
        {"id": "r", "effect": "maybe", "actions": ["filesystem.read"], "resources": ["workspace:**"]},
        {"id": "r", "effect": "allow", "actions": [], "resources": ["workspace:**"]},
        {"id": "r", "effect": "allow", "actions": ["bogus.category"], "resources": ["workspace:**"]},
        {"id": "r", "effect": "allow", "actions": ["filesystem.read"], "resources": []},
    ],
)
def test_compile_rule_rejects_malformed(raw: dict) -> None:
    with pytest.raises(PolicyConfigError):
        compile_rule(raw)


def test_compile_rules_rejects_duplicate_ids() -> None:
    dup = [
        {"id": "x", "effect": "allow", "actions": ["filesystem.read"], "resources": ["workspace:**"]},
        {"id": "x", "effect": "deny", "actions": ["filesystem.read"], "resources": ["workspace:**"]},
    ]
    with pytest.raises(PolicyConfigError):
        compile_rules(dup)


# --------------------------------------------------------------------------- precedence


def _engine(rules: list[dict], fallback: str = "ask") -> PolicyEngine:
    return PolicyEngine(compile_rules(rules), fallback)  # type: ignore[arg-type]


def test_deny_overrides_allow() -> None:
    eng = _engine(
        [
            {"id": "allow-ws", "effect": "allow", "actions": ["filesystem.write"], "resources": ["workspace:**"]},
            {"id": "deny-cfg", "effect": "deny", "actions": ["filesystem.write"], "resources": ["workspace:secret/**"]},
        ]
    )
    assert eng.evaluate("filesystem.write", "workspace:notes/a.md").effect == "allow"
    d = eng.evaluate("filesystem.write", "workspace:secret/x")
    assert d.effect == "deny" and d.matched_rule_id == "deny-cfg"


def test_ask_beats_allow_but_loses_to_deny() -> None:
    eng = _engine(
        [
            {"id": "a", "effect": "allow", "actions": ["network.request"], "resources": ["**"]},
            {"id": "b", "effect": "ask", "actions": ["network.request"], "resources": ["url:https://**"]},
        ]
    )
    assert eng.evaluate("network.request", "url:https://x.test/").effect == "ask"


def test_fallback_used_when_no_match() -> None:
    eng = _engine(
        [{"id": "a", "effect": "allow", "actions": ["filesystem.read"], "resources": ["workspace:**"]}],
        fallback="deny",
    )
    d = eng.evaluate("shell.execute", "workspace:x")
    assert d.effect == "deny" and d.matched_rule_id is None and "fallback" in d.reason


# --------------------------------------------------------------------------- modes


def test_mode_trusted_allows_everything() -> None:
    eng = PolicyEngine.for_mode("trusted")
    assert eng.evaluate("browser.download", "url:https://x.test/f").effect == "allow"
    assert eng.evaluate("filesystem.delete", "config:settings.json").effect == "allow"


def test_mode_standard_posture() -> None:
    eng = PolicyEngine.for_mode("standard")
    assert eng.evaluate("filesystem.write", "workspace:a.md").effect == "allow"
    assert eng.evaluate("filesystem.write", "config:settings.json").effect == "deny"
    assert eng.evaluate("browser.download", "url:https://x.test/f").effect == "ask"
    # unknown action falls to the ask fallback
    assert eng.evaluate("shell.execute", "workspace:a").effect == "ask"


def test_mode_locked_denies_by_default() -> None:
    eng = PolicyEngine.for_mode("locked")
    assert eng.evaluate("filesystem.read", "workspace:a").effect == "allow"
    assert eng.evaluate("filesystem.write", "workspace:a").effect == "deny"
    assert eng.evaluate("browser.download", "url:https://x.test/f").effect == "deny"


def test_extra_rules_layer_on_baseline() -> None:
    extra = compile_rules(
        [{"id": "grant", "effect": "allow", "actions": ["browser.download"], "resources": ["url:https://ok.test/**"]}]
    )
    eng = PolicyEngine.for_mode("standard", extra_rules=extra)
    assert eng.evaluate("browser.download", "url:https://ok.test/f").effect == "allow"
    assert eng.evaluate("browser.download", "url:https://other.test/f").effect == "ask"


def test_decision_to_audit_has_no_secret_and_carries_ids() -> None:
    eng = PolicyEngine.for_mode("standard")
    d = eng.evaluate("credential.use", "secret:ref_123")
    ev = d.to_audit(request_id="req_1", execution_id="ex_1", agent_id="ag_1").to_dict()
    assert ev["event"] == "policy.decision" and ev["effect"] == "ask"
    assert ev["resource"] == "secret:ref_123"  # ref only, never a secret value
    assert ev["request_id"] == "req_1" and ev["agent_id"] == "ag_1"
    assert "session_id" not in ev  # unset correlation ids omitted
