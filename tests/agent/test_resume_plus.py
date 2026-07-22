"""Resume Plus Phase 1 — resolver, run identity, run context, and envelope store.

``config_home`` (autouse from tests/conftest) roots the registry/runs/projects dirs in a tmp dir, so
these exercise the resolver's fail-closed semantics and the on-disk seams for real.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tabvis.agent import run_context as RC
from tabvis.agent import run_envelope as RE
from tabvis.agent.agents import registry
from tabvis.agent.resume_plus import ResumeError, ResumeErrorCode, resolve_resume
from tabvis.utils.env_utils import get_tabvis_config_home_dir


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    from tabvis.utils import session_storage as ss

    registry._records.clear()
    registry._persisted_loaded = True  # skip disk load; tests populate _records directly
    # get_project_dir memoizes cwd -> path; each test gets a fresh tmp config_home, so a stale entry
    # would point writes at a previous test's (now-gone) projects dir. Clear it around each case.
    ss._project_dir_cache.clear()
    yield
    registry._records.clear()
    registry._persisted_loaded = False
    ss._project_dir_cache.clear()


def _make_agent(session_id: str, *, agent_id: str = "ag_x", cwd: str = "/work",
                profile: str | None = "default", status: str = "completed",
                principal: str = registry.LOCAL_PRINCIPAL) -> registry.AgentRecord:
    rec = registry.AgentRecord(
        agent_id=agent_id, session_id=session_id, run_id=registry.new_run_id(),
        principal_id=principal, cwd=cwd, profile=profile, status=status,
    )
    registry._records[agent_id] = rec
    return rec


def _write_transcript(session_id: str, cwd: str) -> str:
    """Create a fake transcript file under the project dir for ``cwd`` and return the project dir."""
    from tabvis.utils.session_storage import get_project_dir

    pdir = get_project_dir(cwd)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, f"{session_id}.jsonl"), "w") as fh:
        fh.write('{"type":"user"}\n')
    return pdir


# --------------------------------------------------------------------------- run identity


def test_new_run_id_unique_and_prefixed() -> None:
    a, b = registry.new_run_id(), registry.new_run_id()
    assert a.startswith("run_") and b.startswith("run_") and a != b


def test_create_and_reuse_get_distinct_run_ids() -> None:
    rec = registry.create(session_id="s1", prompt="p", cwd="/w")
    first = rec.run_id
    assert first.startswith("run_")
    registry.reuse(rec.agent_id, prompt="p2")
    assert rec.run_id != first  # a reuse is a new execution (§4.3)


# --------------------------------------------------------------------------- reverse lookup / guard


def test_find_agents_by_session_scopes_by_principal() -> None:
    _make_agent("sess-A", agent_id="ag_a", principal="principal_local")
    _make_agent("sess-A", agent_id="ag_b", principal="principal_other")
    mine = registry.find_agents_by_session("sess-A", principal_id="principal_local")
    assert [r.agent_id for r in mine] == ["ag_a"]
    everyone = registry.find_agents_by_session("sess-A")
    assert len(everyone) == 2


def test_active_run_guard() -> None:
    _make_agent("s", agent_id="ag_run", status="running")
    assert registry.active_run("ag_run") is not None
    registry._records["ag_run"].status = "completed"
    assert registry.active_run("ag_run") is None


# --------------------------------------------------------------------------- resolver: fail closed


def test_resolve_unknown_session_not_found() -> None:
    with pytest.raises(ResumeError) as ei:
        resolve_resume("11111111-2222-3333-4444-555555555555", current_cwd="/work")
    assert ei.value.code == ResumeErrorCode.SESSION_NOT_FOUND


def test_resolve_invalid_selector() -> None:
    with pytest.raises(ResumeError) as ei:
        resolve_resume("../etc/passwd", current_cwd="/work")
    assert ei.value.code == ResumeErrorCode.INVALID_SELECTOR


def test_resolve_ambiguous() -> None:
    _make_agent("dupe", agent_id="ag_1")
    _make_agent("dupe", agent_id="ag_2")
    with pytest.raises(ResumeError) as ei:
        resolve_resume("dupe", current_cwd="/work")
    assert ei.value.code == ResumeErrorCode.SESSION_AMBIGUOUS


def test_resolve_forbidden_cross_principal() -> None:
    _make_agent("owned", agent_id="ag_o", principal="principal_other")
    with pytest.raises(ResumeError) as ei:
        resolve_resume("owned", principal_id="principal_local", current_cwd="/work")
    assert ei.value.code == ResumeErrorCode.FORBIDDEN


def test_resolve_active_run_conflicts() -> None:
    _make_agent("busy", agent_id="ag_busy", status="running", cwd="/work")
    with pytest.raises(ResumeError) as ei:
        resolve_resume("busy", current_cwd="/work")
    assert ei.value.code == ResumeErrorCode.AGENT_RUN_ACTIVE


def test_resolve_cwd_mismatch_rejected_by_default() -> None:
    _make_agent("elsewhere", agent_id="ag_e", cwd="/other/dir")
    with pytest.raises(ResumeError) as ei:
        resolve_resume("elsewhere", current_cwd="/work")
    assert ei.value.code == ResumeErrorCode.IDENTITY_MISMATCH
    # ...but allow_cwd_change lets it through
    t = resolve_resume("elsewhere", current_cwd="/work", allow_cwd_change=True)
    assert t.agent_id == "ag_e"


# --------------------------------------------------------------------------- resolver: success


def test_resolve_registry_agent() -> None:
    _make_agent("good", agent_id="ag_g", cwd="/work", profile="default")
    pdir = _write_transcript("good", "/work")
    t = resolve_resume("good", current_cwd="/work")
    assert t.agent_id == "ag_g" and t.session_id == "good"
    assert t.run_id.startswith("run_") and t.profile == "default"
    assert t.project_dir == pdir
    assert t.browser_recovery == "relaunched_profile"  # not resident
    assert t.read_memory is True and t.write_memory is True  # plus mode


def test_resolve_conversation_only_disables_memory() -> None:
    _make_agent("c", agent_id="ag_c", cwd="/work")
    _write_transcript("c", "/work")
    t = resolve_resume("c", mode="conversation_only", current_cwd="/work")
    assert t.read_memory is False and t.write_memory is False


def test_resolve_cli_transcript_only_session() -> None:
    # No durable agent record — resolve a bare CLI transcript by locating its project dir.
    pdir = _write_transcript("cli-sess-1", "/work")
    t = resolve_resume("cli-sess-1", current_cwd="/work")
    assert t.agent_id == "default" and t.profile == "default"
    assert t.project_dir == pdir
    assert t.browser_recovery == "relaunched_profile"


def test_resolve_never_claims_resident_for_oneshot() -> None:
    _write_transcript("cli-sess-2", "/work")
    t = resolve_resume("cli-sess-2", current_cwd="/work", resident=False)
    assert t.browser_recovery != "attached_live"


# --------------------------------------------------------------------------- run context


def test_run_context_scope_binds_and_restores() -> None:
    assert RC.get_run_context() is None
    ctx = RC.RunContext(principal_id="p", agent_id="a", session_id="s", run_id="run_1", cwd="/w")
    with RC.run_context_scope(ctx) as bound:
        assert bound is ctx and RC.get_run_context() is ctx
    assert RC.get_run_context() is None


# --------------------------------------------------------------------------- run envelope


def test_run_envelope_roundtrip_and_command_idempotency() -> None:
    env = RE.RunEnvelope(run_id="run_abc", agent_id="ag", session_id="s",
                         command_id="cmd-1", resume_mode="plus")
    RE.save(env)
    assert os.path.exists(os.path.join(RE.runs_dir(), "run_abc.json"))
    loaded = RE.load("run_abc")
    assert loaded is not None and loaded.resume_mode == "plus"
    # a retried create with the same command id maps back to the original run
    assert RE.find_by_command("cmd-1").run_id == "run_abc"
    assert RE.find_by_command("nope") is None


def test_run_envelope_terminal_transition() -> None:
    RE.save(RE.RunEnvelope(run_id="run_t", agent_id="ag", session_id="s"))
    RE.mark_started("run_t")
    RE.mark_terminal("run_t", "completed", evidence_checkpoint_ref="checkpoint:run_t")
    env = RE.load("run_t")
    assert env.status == "completed" and env.started_at and env.ended_at
    assert env.evidence_checkpoint_ref == "checkpoint:run_t"


def test_command_index_sanitizes_key() -> None:
    # a hostile command id cannot escape the runs dir
    env = RE.RunEnvelope(run_id="run_s", agent_id="ag", session_id="s", command_id="../../evil")
    RE.save(env)
    found = RE.find_by_command("../../evil")
    assert found is not None and found.run_id == "run_s"
    # nothing was written outside the runs dir
    assert os.path.commonpath([
        os.path.realpath(RE._command_index_path("../../evil")),
        os.path.realpath(RE.runs_dir()),
    ]) == os.path.realpath(RE.runs_dir())


def test_config_home_is_tmp() -> None:
    # sanity: the autouse config_home fixture isolated us
    assert "tabvis-config" in get_tabvis_config_home_dir()
