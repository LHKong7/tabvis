"""Integration glue — run-completion → consolidation (design §7.2, §10.4).

Covers the shared consolidate_after_run entry point (gating, evidence build, extractor path,
never-raises) and the default extractor's JSON parsing + feature gate. The stream_agent wiring is
gated off by default (TABVIS_BROWSER_MEMORY cleared by the autouse conftest fixture), so existing
run behavior is unchanged — verified by the untouched full suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from tabvis.agent.mem import agent_store as A
from tabvis.agent.mem import extractor as E
from tabvis.agent.mem.agent_store import AgentMemoryStore
from tabvis.agent.mem.integration import consolidate_after_run


@pytest.fixture(autouse=True)
def _clean() -> Any:
    A._locks.clear()
    yield
    A._locks.clear()


_MSGS = [{"type": "user", "uuid": "u1", "message": {"content": "Prefer primary docs."}}]
_ARTS = [{"seq": 1, "type": "navigation", "url": "https://example.com/x", "title": "X"}]


def _consented(agent: str = "ag_1") -> AgentMemoryStore:
    s = AgentMemoryStore("principal_local", agent)
    s.grant_consent()
    return s


async def _fake_extractor(_packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "sessionDigest": {"goal": "Compare"},
        "userFacts": [{"statement": "Prefer primary docs.", "sourceMessageUuid": "u1", "explicit": True}],
        "browsingTopics": [],
    }


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _call(store_agent: str = "ag_1", **kw: Any) -> Any:
    base = dict(principal_id="principal_local", agent_id=store_agent, session_id="s1",
                run_id="run_1", status="completed", messages=_MSGS, artifacts=_ARTS)
    base.update(kw)
    return _run(consolidate_after_run(**base))


# --------------------------------------------------------------------------- feature gate


def test_noop_when_feature_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABVIS_BROWSER_MEMORY", raising=False)
    _consented()
    assert _call(extractor=_fake_extractor) is None


def test_noop_when_write_memory_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    _consented()
    assert _call(extractor=_fake_extractor, write_memory=False) is None


def test_noop_when_no_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    AgentMemoryStore("principal_local", "ag_nc")  # no grant_consent
    assert _call("ag_nc", extractor=_fake_extractor) is None


# --------------------------------------------------------------------------- happy path


def test_consolidates_when_enabled_and_consented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    store = _consented()
    res = _call(extractor=_fake_extractor)
    assert res is not None and res.status == "committed"
    assert [f.text for f in store.get_effective_snapshot().user_facts] == ["Prefer primary docs."]


def test_default_extractor_used_when_none_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    store = _consented()
    # no extractor arg → get_extractor(); patch it to our fake so no real model is called
    monkeypatch.setattr(E, "default_extractor", _fake_extractor)
    res = _call()  # extractor defaults to the configured one
    assert res is not None and res.status == "committed"
    assert len(store.get_effective_snapshot().user_facts) == 1


def test_records_session_digest_without_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    store = _consented()
    res = _call(extractor=None, status="interrupted")
    assert res is not None and res.status == "committed"
    sessions = store.get_effective_snapshot().sessions
    assert sessions and sessions[0].status == "interrupted"


def test_never_raises_on_bad_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    _consented()

    async def boom(_p: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("model exploded")

    # consolidate_after_run returns a result (failed) rather than raising
    res = _call(extractor=boom)
    assert res is not None and res.status == "failed"


# --------------------------------------------------------------------------- extractor unit


def test_is_browser_memory_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABVIS_BROWSER_MEMORY", raising=False)
    assert E.is_browser_memory_enabled() is False
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    assert E.is_browser_memory_enabled() is True


def test_get_extractor_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABVIS_BROWSER_MEMORY", raising=False)
    assert E.get_extractor() is None
    monkeypatch.setenv("TABVIS_BROWSER_MEMORY", "1")
    assert E.get_extractor() is E.default_extractor


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"userFacts": []}', {"userFacts": []}),
        ('```json\n{"userFacts": []}\n```', {"userFacts": []}),
        ('```\n{"userFacts": [1]}\n```', {"userFacts": [1]}),
        ('Here: {"a": 1} done', {"a": 1}),
    ],
)
def test_parse_candidate_json(text: str, expected: dict) -> None:
    assert E.parse_candidate_json(text) == expected


def test_parse_candidate_json_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        E.parse_candidate_json("[1, 2, 3]")
    with pytest.raises(ValueError):
        E.parse_candidate_json("not json at all")
