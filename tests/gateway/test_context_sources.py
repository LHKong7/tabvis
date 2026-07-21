"""Context providers → live subsystems bridge (design §11.3).

`SourceCollector` gathers the real tabvis subsystems into a `ContextRequest.sources` snapshot, which the
(unchanged, deterministic) Context Runtime then assembles. Here the source hooks are fakes so the
collector is exercised without a real project/browser, plus one test that the default path calls the
real loader, plus graceful-degradation coverage.
"""

from __future__ import annotations

import asyncio

import pytest

from tabvis.gateway.runtime.context.pack import SECRET_REF
from tabvis.gateway.runtime.context.runtime import ContextRuntime
from tabvis.gateway.runtime.context.sources import SourceCollector


async def _const(value):
    return value


def test_injected_sources_populate_the_request() -> None:
    async def scenario() -> None:
        collector = SourceCollector(
            project_instructions=lambda: _const("Use TABVIS conventions."),
            memory=lambda: _const("prefers concise answers"),
            git_status=lambda: _const("branch: main\nclean"),
            browser_summary=lambda agent_id: {"url": "https://ex.com", "title": "Ex"},
            agent_definition="research agent",
            tool_descriptors=[{"name": "click"}],
        )
        req = await collector.collect(run_id="run_1", session_id="ses_1", agent_id="ag_1", model="m")
        assert req.sources["project_instructions"]["text"] == "Use TABVIS conventions."
        assert req.sources["memory"] == "prefers concise answers"
        assert req.sources["workspace"].startswith("branch: main")
        assert req.sources["browser_snapshot"]["url"] == "https://ex.com"
        assert req.sources["agent_definition"] == "research agent"
        assert req.sources["tool_descriptors"] == [{"name": "click"}]

        pack = ContextRuntime().build(req)
        providers = [s["provider_id"] for s in pack.system_sections]
        assert "project_instructions" in providers and "memory" in providers and "browser" in providers

    asyncio.run(scenario())


def test_absent_sources_are_simply_omitted() -> None:
    async def scenario() -> None:
        # No hooks and no agent → the real loaders run but nothing is configured in a bare test env;
        # collection still succeeds and just yields a sparse snapshot.
        collector = SourceCollector(
            project_instructions=lambda: _const(None),
            memory=lambda: _const(None),
            git_status=lambda: _const(None),
        )
        req = await collector.collect(run_id="run_1", session_id="ses_1", model="m")
        assert "project_instructions" not in req.sources
        assert "memory" not in req.sources
        assert "workspace" not in req.sources

    asyncio.run(scenario())


def test_a_failing_source_degrades_without_breaking_the_rest() -> None:
    async def scenario() -> None:
        async def boom():
            raise RuntimeError("memory subsystem down")

        collector = SourceCollector(
            project_instructions=lambda: _const("instructions"),
            memory=boom,   # this one blows up
            git_status=lambda: _const(None),
        )
        req = await collector.collect(run_id="run_1", session_id="ses_1", model="m")
        assert req.sources["project_instructions"]["text"] == "instructions"  # survived
        assert "memory" not in req.sources                                    # degraded, omitted

    asyncio.run(scenario())


def test_default_path_calls_the_real_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        import tabvis.agent.project_instructions as pi

        async def fake_loader(additional_directories=None):
            return "REAL project instructions"

        monkeypatch.setattr(pi, "load_project_instructions_prompt", fake_loader)
        # no injected hook → the collector calls the real (now patched) loader.
        req = await SourceCollector(memory=lambda: _const(None), git_status=lambda: _const(None)).collect(
            run_id="run_1", session_id="ses_1", model="m"
        )
        assert req.sources["project_instructions"]["text"] == "REAL project instructions"

    asyncio.run(scenario())


def test_transcript_source_only_when_a_loader_is_provided() -> None:
    async def scenario() -> None:
        base = dict(run_id="run_1", session_id="ses_1", model="m")
        no_tx = await SourceCollector(
            project_instructions=lambda: _const(None), memory=lambda: _const(None),
            git_status=lambda: _const(None),
        ).collect(**base)
        assert "transcript" not in no_tx.sources

        async def load_tx(session_id):
            return [{"id": "m1", "role": "user", "text": "hi"}]

        with_tx = await SourceCollector(
            project_instructions=lambda: _const(None), memory=lambda: _const(None),
            git_status=lambda: _const(None), transcript=load_tx,
        ).collect(**base)
        assert with_tx.sources["transcript"][0]["text"] == "hi"

    asyncio.run(scenario())


def test_build_pack_end_to_end_keeps_secrets_out() -> None:
    async def scenario() -> None:
        collector = SourceCollector(
            project_instructions=lambda: _const("instructions"),
            memory=lambda: _const(None), git_status=lambda: _const(None),
            browser_summary=lambda agent_id: {"url": "u"},
            browser_credential_ref="secret://browser/cookies",
        )
        pack = await collector.build_pack(
            runtime=ContextRuntime(), run_id="run_1", session_id="ses_1", agent_id="ag_1", model="m"
        )
        assert pack.digest
        cred = [s for s in pack.system_sections if s["sensitivity"] == SECRET_REF]
        assert len(cred) == 1 and cred[0]["content"] == "secret://browser/cookies"

    asyncio.run(scenario())
