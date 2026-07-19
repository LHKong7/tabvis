"""Tests for ``tabvis.browser.manager`` — the **agent-bundled** browser workspace model.

The manager's job since browsers were bundled to agents: an agent reserves a workspace at spawn and
owns it for its whole life. That workspace is not released at the end of a run and is never
idle-reaped — only a quit/close ends the bundle. These tests exercise that ownership/lifetime logic
directly, without launching a real browser (a workspace's ``service`` stays ``None``; ownership is
independent of whether Chromium was ever launched).

State lives in module globals (``_workspaces`` / ``_slots``), so a fixture clears them per test.
``persist=False`` keeps every session pure — no browser-session.json is written to disk. Async
manager coroutines are driven with ``asyncio.run`` so the suite stays synchronous (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from tabvis.browser import manager as m


@pytest.fixture(autouse=True)
def _clean_manager_state() -> Any:
    """Wipe the manager's module globals around each test (they persist for the process otherwise)."""
    m._workspaces.clear()
    m._slots.clear()
    yield
    m._workspaces.clear()
    m._slots.clear()


def _spawn(agent_id: str, profile: str | None = "default") -> str:
    """Bundle a workspace to an agent (no disk writes) and return its user_data_dir."""
    m.init_browser_session(
        session_id=f"sess-{agent_id}",
        model="test-model",
        cwd="/tmp",
        agent_id=agent_id,
        profile=profile,
        persist=False,
    )
    return m.resolve_profile_dir(agent_id, profile)


class _FakeService:
    """Just enough of BrowserService for the reaper/introspection paths (never really launches)."""

    def __init__(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def tabs(self) -> list[dict[str, Any]]:
        return []

    def browser_info(self) -> dict[str, Any]:
        return {"engine": "chromium"}

    async def close(self) -> None:
        self._alive = False


# --------------------------------------------------------------------------- reserve at spawn


def test_spawn_reserves_owner_and_busy() -> None:
    """An agent bundles its workspace at spawn: it both OWNS it and holds the driving claim."""
    dd = _spawn("A")
    ws = m._workspaces[dd]
    assert ws.owner_agent == "A"
    assert ws.busy_agent == "A"
    assert m.get_workspace_owner(dd) == "A"


def test_second_agent_cannot_bundle_an_owned_profile() -> None:
    """A profile is bundled to its owner for life, so another agent is refused — not queued."""
    _spawn("A", profile="default")
    with pytest.raises(RuntimeError, match="bundled to agent 'A'"):
        _spawn("B", profile="default")


def test_isolated_agents_never_collide() -> None:
    """An omitted profile is per-agent, so two agents get different dirs and both bundle fine."""
    a = _spawn("A", profile=None)
    b = _spawn("B", profile=None)
    assert a != b
    assert m.get_workspace_owner(a) == "A"
    assert m.get_workspace_owner(b) == "B"


# --------------------------------------------------------------------------- held across runs


def test_detach_keeps_the_bundle_open() -> None:
    """End of a run drops only the *driving* claim; the bundle (owner) and browser stay."""
    dd = _spawn("A")
    ws = m._workspaces[dd]

    asyncio.run(m.detach_agent("A"))

    assert ws.owner_agent == "A"           # still bundled
    assert ws.busy_agent is None           # no longer actively driving
    assert m.get_workspace_owner(dd) == "A"
    assert m.get_profile_holder(dd) is None  # nothing mid-run, so a quit could proceed


def test_owner_still_refuses_others_between_runs() -> None:
    """Because the bundle outlives the run, a second agent is refused even while nobody is driving."""
    _spawn("A")
    asyncio.run(m.detach_agent("A"))  # run over, but A still owns the profile
    with pytest.raises(RuntimeError, match="bundled to agent 'A'"):
        _spawn("B", profile="default")


def test_same_agent_may_re_run_its_bundle() -> None:
    """The owner coming back for another run re-affirms the claim rather than being refused."""
    dd = _spawn("A")
    # A second spawn for the SAME agent (a follow-up run) must succeed and re-take the wheel.
    m.init_browser_session(
        session_id="sess-A2", model="m", cwd="/tmp", agent_id="A", profile="default", persist=False
    )
    ws = m._workspaces[dd]
    assert ws.owner_agent == "A"
    assert ws.busy_agent == "A"


# --------------------------------------------------------------------------- quit ends the bundle


def test_quit_releases_a_never_launched_bundle() -> None:
    """Quitting frees the profile even if no browser was ever launched behind the reservation."""
    dd = _spawn("A")
    closed = asyncio.run(m.quit_agent_browser("A"))
    assert closed is False                 # nothing was actually open to close
    ws = m._workspaces[dd]
    assert ws.owner_agent is None          # ...but the bundle is released
    assert ws.busy_agent is None
    # The profile is now free for a new agent.
    _spawn("B", profile="default")
    assert m.get_workspace_owner(dd) == "B"


def test_quit_closes_a_launched_browser_and_frees_it() -> None:
    """With a live browser, quit closes it, reports True, and clears the bundle."""
    dd = _spawn("A")
    ws = m._workspaces[dd]
    fake = _FakeService()
    ws.service = fake

    closed = asyncio.run(m.quit_agent_browser("A"))

    assert closed is True
    assert fake.is_alive() is False        # the browser was actually closed
    assert ws.service is None
    assert ws.owner_agent is None


def test_quit_unknown_agent_is_a_noop() -> None:
    assert asyncio.run(m.quit_agent_browser("nope")) is False


# --------------------------------------------------------------------------- no idle reaping


def test_bundled_workspace_is_never_reapable() -> None:
    """The core lifetime guarantee: an owned workspace is never idle-reaped, however long it sits."""
    dd = _spawn("A")
    ws = m._workspaces[dd]
    ws.service = _FakeService()
    ws.busy_agent = None            # not mid-run
    ws.last_used_at = 0.0           # ancient — well past any timeout

    # Even with an enormous elapsed idle time, an owned workspace is not reapable.
    assert m._is_reapable(ws, now=10_000_000.0, timeout_ms=1) is False


def test_unowned_idle_workspace_is_reapable() -> None:
    """The reaper still catches a genuinely orphaned workspace (no owner) — e.g. library use."""
    dd = _spawn("A")
    ws = m._workspaces[dd]
    ws.service = _FakeService()
    ws.busy_agent = None
    ws.owner_agent = None           # orphaned
    ws.last_used_at = 0.0

    assert m._is_reapable(ws, now=10_000_000.0, timeout_ms=1) is True


def test_actively_driven_workspace_is_not_reapable() -> None:
    dd = _spawn("A")
    ws = m._workspaces[dd]
    ws.service = _FakeService()
    ws.owner_agent = None           # even unowned...
    ws.busy_agent = "A"             # ...an actively-driving run is never reaped
    ws.last_used_at = 0.0
    assert m._is_reapable(ws, now=10_000_000.0, timeout_ms=1) is False


# --------------------------------------------------------------------------- introspection


def test_list_workspaces_reports_ownership() -> None:
    dd = _spawn("A")
    m._workspaces[dd].service = _FakeService()
    rows = m.list_workspaces()
    assert len(rows) == 1
    row = rows[0]
    assert row["owner_agent"] == "A"
    assert row["bundled"] is True


# --------------------------------------------------------------------------- profile ↔ agent is 1:1


def test_profile_name_is_a_stable_identity() -> None:
    """The same profile name maps to the same dir regardless of which agent_id asks — so a
    persistent agent re-attaches to its browser across runs (different run ids, same profile)."""
    d1 = m.resolve_profile_dir("ag_run1", "work")
    d2 = m.resolve_profile_dir("ag_run2", "work")
    assert d1 == d2
    assert d1.endswith(os.path.join("profiles", "work"))


def test_distinct_profiles_get_distinct_dirs() -> None:
    assert m.resolve_profile_dir("x", "work") != m.resolve_profile_dir("x", "play")


def test_default_profile_is_the_base_dir() -> None:
    base = m.resolve_profile_dir("anyone", "default")
    assert base == m.get_browser_user_data_dir()
    # A named profile lives UNDER the base, never at it.
    assert m.resolve_profile_dir("x", "work") != base


def test_no_profile_falls_back_to_the_agent_id_identity() -> None:
    """Omitting a profile still yields a 1:1 per-agent dir (keyed by the agent's own id)."""
    assert m.resolve_profile_dir("ag_abc", None).endswith(os.path.join("profiles", "ag_abc"))


def test_profile_identity_cannot_escape_the_profiles_dir() -> None:
    """A hostile profile name must stay confined under <base>/profiles (no path traversal)."""
    base = m.get_browser_user_data_dir()
    for hostile in ["../../etc", "../..", "a/b/c", "..", "  "]:
        d = m.resolve_profile_dir("ag", hostile)
        assert d.startswith(os.path.join(base, "profiles") + os.sep)
        assert ".." not in os.path.relpath(d, base).split(os.sep)
        assert d != base


def test_agent_cannot_rebind_to_a_second_profile() -> None:
    """The agent→profile direction of the 1:1: once bound, an agent may not switch profiles."""
    _spawn("A", profile="work")
    with pytest.raises(RuntimeError, match="already bound to profile"):
        _spawn("A", profile="play")


def test_same_agent_same_profile_reattaches() -> None:
    """Re-running the same agent with the SAME profile is allowed (it re-attaches, 1:1 intact)."""
    d1 = _spawn("A", profile="work")
    d2 = _spawn("A", profile="work")  # a follow-up run
    assert d1 == d2
    assert m.get_workspace_owner(d1) == "A"
