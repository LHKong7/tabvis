"""PP-8 — Bash adapter: shell.execute + best-effort network.request over the existing resolver.

The engine layers on top of the rich bash permission resolver and the composed decision is the most
restrictive of the two. Default (standard, non-strict) preserves behavior; locked denies shell by
default; settings/grants and strict-mode network gating add enforcement. Every command is audited.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.policy import audit as policy_audit
from tabvis.policy import bash_adapter, grants
from tabvis.policy.bash_adapter import evaluate, evaluate_command, is_bash_strict
from tabvis.policy.rules import compile_rules
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    for var in ("TABVIS_PERMISSION_MODE", "TABVIS_PERMISSION_SHADOW", "TABVIS_PERMISSION_BASH_STRICT"):
        monkeypatch.delenv(var, raising=False)
    grants.clear()
    reset_settings_cache()
    yield
    grants.clear()
    reset_settings_cache()


def _ctx(agent_id: str = "agB") -> Any:
    return SimpleNamespace(agent_id=agent_id, tool_use_id="tu_1", command="")


# --------------------------------------------------------------------------- engine-only decision


def test_standard_allows_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    assert evaluate_command("ls -la", _ctx())["behavior"] == "allow"


def test_locked_denies_shell_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "locked")
    d = evaluate_command("ls -la", _ctx())
    assert d["behavior"] == "deny"
    assert d["decisionReason"]["action"] == "shell.execute"


def test_settings_rule_can_deny_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    deny_rm = compile_rules([{"id": "no-rm", "effect": "deny", "actions": ["shell.execute"], "resources": ["shell:rm"]}])
    monkeypatch.setattr(bash_adapter, "load_policy_rules_from_settings", lambda: deny_rm)
    assert evaluate_command("rm -rf build", _ctx())["behavior"] == "deny"
    assert evaluate_command("ls", _ctx())["behavior"] == "allow"  # other commands unaffected


# --------------------------------------------------------------------------- network heuristic


def test_network_allowed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert evaluate_command("curl https://x.test/f", _ctx())["behavior"] == "allow"


def test_strict_network_asks_then_grant_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_BASH_STRICT", "1")
    assert is_bash_strict() is True
    # standard fallback for an unmatched network.request is ask
    assert evaluate_command("curl https://ok.test/f", _ctx())["behavior"] == "ask"
    # non-network command still allowed
    assert evaluate_command("ls", _ctx())["behavior"] == "allow"
    # grant the host → allowed
    grants.add_grant("network.request", "url:https://ok.test/**", agent_id="agB")
    assert evaluate_command("curl https://ok.test/data", _ctx())["behavior"] == "allow"


# --------------------------------------------------------------------------- composition with resolver


def _stub_resolver(monkeypatch: pytest.MonkeyPatch, decision: dict[str, Any]) -> None:
    """Stub the rich bash resolver so composition is tested in isolation from its app_state needs."""
    import tabvis.agent.tools.bash_permissions as bp

    async def _fake(_input: Any, _ctx: Any, **_kw: Any) -> dict[str, Any]:
        return decision

    monkeypatch.setattr(bp, "bash_tool_has_permission", _fake)


def test_compose_engine_deny_overrides_resolver_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "locked")
    _stub_resolver(monkeypatch, {"behavior": "allow", "updatedInput": {}})
    d = asyncio.run(evaluate(SimpleNamespace(command="ls"), _ctx()))
    assert d["behavior"] == "deny"  # engine (locked) is more restrictive than the resolver's allow


def test_compose_preserves_resolver_when_engine_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    # standard, non-strict: engine allows, so the resolver's own decision stands verbatim.
    sentinel = {"behavior": "ask", "message": "resolver says ask", "updatedInput": {}}
    _stub_resolver(monkeypatch, sentinel)
    d = asyncio.run(evaluate(SimpleNamespace(command="ls"), _ctx()))
    assert d is sentinel  # resolver's richer decision preserved unchanged


def test_compose_resolver_deny_wins_over_engine_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    # resolver's fine-grained deny is never loosened by the engine's allow.
    deny = {"behavior": "deny", "message": "resolver blocked rm", "decisionReason": {}}
    _stub_resolver(monkeypatch, deny)
    d = asyncio.run(evaluate(SimpleNamespace(command="rm -rf x"), _ctx()))
    assert d is deny


# --------------------------------------------------------------------------- audit + shadow


def test_command_is_audited() -> None:
    records: list[dict[str, Any]] = []
    unsub = policy_audit.register_sink(records.append)
    try:
        evaluate_command("ls", _ctx("agX"))
    finally:
        unsub()
    assert records and records[-1]["event"] == "policy.decision"
    assert records[-1]["agent_id"] == "agX" and records[-1]["action"] == "shell.execute"


def test_shadow_serves_locked_deny_as_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "locked")
    monkeypatch.setenv("TABVIS_PERMISSION_SHADOW", "1")
    d = evaluate_command("ls", _ctx())
    assert d["behavior"] == "allow" and d["decisionReason"]["wouldBe"] == "deny"
