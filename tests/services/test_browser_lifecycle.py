"""Phase 5 — distinct lifecycle operations + profile generation (design §14.1, §6.2).

The headline acceptance: quit / suspend / clear-profile / forget-memory / delete-agent are separate
and isolated — none silently does another's job. No real Chromium: the manager primitives are
monkeypatched; the memory store and profile clearing run for real against the tmp config home.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from tabvis.agent.mem import agent_store as A
from tabvis.agent.mem.agent_store import AgentMemoryStore
from tabvis.agent.mem.schemas import MemorySnapshot, UserFact
from tabvis.browser import lifecycle, profile_generation
from tabvis.utils.env_utils import get_tabvis_config_home_dir


@pytest.fixture(autouse=True)
def _clean() -> Any:
    A._locks.clear()
    yield
    A._locks.clear()


def _memory(agent: str = "ag_1", fact: str = "Prefer concise answers.") -> AgentMemoryStore:
    s = AgentMemoryStore("principal_local", agent)
    s.grant_consent()
    s.commit(MemorySnapshot(user_facts=[UserFact.create(fact)]))
    return s


def _patch_manager(monkeypatch: pytest.MonkeyPatch, *, closed: bool = True,
                   profile_dir: str | None = None) -> None:
    from tabvis.browser import manager

    async def _quit(_agent_id: str) -> bool:
        return closed

    monkeypatch.setattr(manager, "quit_agent_browser", _quit)
    if profile_dir is not None:
        monkeypatch.setattr(manager, "resolve_profile_dir", lambda a, p: profile_dir)


# --------------------------------------------------------------------------- profile generation


def test_profile_generation_bump_and_history() -> None:
    assert profile_generation.current("agX") == 0
    rec = profile_generation.bump("agX", reason="clear_profile")
    assert rec.generation == 1 and rec.reason == "clear_profile"
    profile_generation.bump("agX", reason="reset")
    info = profile_generation.info("agX")
    assert info.generation == 2 and len(info.history) == 2


# --------------------------------------------------------------------------- quit / suspend


def test_quit_browser_keeps_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_manager(monkeypatch, closed=True)
    store = _memory()
    res = asyncio.run(lifecycle.quit_browser("ag_1"))
    assert res["browser_closed"] is True
    assert res["profile_kept"] and res["memory_kept"] and res["evidence_kept"]
    # memory untouched by a quit
    assert len(store.get_effective_snapshot().user_facts) == 1


def test_suspend_keeps_binding_and_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_manager(monkeypatch, closed=True)
    res = asyncio.run(lifecycle.suspend_agent("ag_1"))
    assert res["suspended"] and res["memory_kept"] and res["profile_kept"]


# --------------------------------------------------------------------------- forget ≠ logout


def test_forget_memory_touches_only_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.browser import manager

    # If forget touched the browser at all, this would blow up — it must not be called.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("forget_memory must not touch the browser")

    monkeypatch.setattr(manager, "quit_agent_browser", _boom)
    store = _memory()
    fid = store.get_effective_snapshot().user_facts[0].id
    res = lifecycle.forget_memory("principal_local", "ag_1", "fact", fid)
    assert res["browser_untouched"] and res["profile_untouched"]
    assert store.get_effective_snapshot().user_facts == []  # forgotten


# --------------------------------------------------------------------------- clear profile ≠ delete memory


def test_clear_profile_keeps_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    profile_dir = os.path.join(get_tabvis_config_home_dir(), "browser-ag1")
    os.makedirs(profile_dir, exist_ok=True)
    open(os.path.join(profile_dir, "Cookies"), "w").close()
    _patch_manager(monkeypatch, closed=False, profile_dir=profile_dir)
    store = _memory()

    res = asyncio.run(lifecycle.clear_profile("ag_1", wait=True))
    assert res["profile_cleared"] is True
    assert res["profile_generation"] == 1        # generation bumped (intentional reset)
    assert res["memory_kept"] is True
    assert not os.path.exists(profile_dir)        # profile gone
    assert len(store.get_effective_snapshot().user_facts) == 1  # memory intact


# --------------------------------------------------------------------------- delete agent (composite)


def test_delete_agent_default_keeps_durable_data(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_manager(monkeypatch, closed=True)
    _memory()
    res = asyncio.run(lifecycle.delete_agent("principal_local", "ag_1"))
    assert res["browser_closed"] is True
    assert res["profile_deleted"] is False and res["memory_deleted"] is False
    # memory NOT deleted without the explicit flag
    assert AgentMemoryStore("principal_local", "ag_1").get_current_revision() is not None


def test_delete_agent_deletes_memory_only_when_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_manager(monkeypatch, closed=True)
    _memory()
    res = asyncio.run(lifecycle.delete_agent("principal_local", "ag_1", delete_memory=True))
    assert res["memory_deleted"] is True
    assert AgentMemoryStore("principal_local", "ag_1").get_current_revision() is None


def test_delete_agent_deletes_profile_only_when_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    profile_dir = os.path.join(get_tabvis_config_home_dir(), "browser-agdel")
    os.makedirs(profile_dir, exist_ok=True)
    _patch_manager(monkeypatch, closed=True, profile_dir=profile_dir)
    store = _memory()
    res = asyncio.run(lifecycle.delete_agent("principal_local", "ag_1", delete_profile=True))
    assert res["profile_deleted"] is True
    assert not os.path.exists(profile_dir)
    # memory kept (only profile flagged)
    assert len(store.get_effective_snapshot().user_facts) == 1


# --------------------------------------------------------------------------- artifacts


def test_delete_artifacts_keeps_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.browser import artifacts

    d = artifacts.get_artifacts_dir("sess-del")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "events.jsonl"), "w").close()
    res = lifecycle.delete_artifacts("sess-del")
    assert res["artifacts_removed"] is True and res["memory_kept"] is True
    assert not os.path.exists(d)


# --------------------------------------------------------------------------- status


def test_browser_status_reports_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.browser import manager

    monkeypatch.setattr(manager, "list_workspaces", lambda: [
        {"bundled": True}, {"bundled": True}, {"bundled": False},
    ])
    monkeypatch.setenv("TABVIS_SERVER_MAX_AGENTS", "2")
    status = lifecycle.browser_status()
    assert status["resident"] == 2 and status["capacity"] == 2 and status["at_capacity"] is True
