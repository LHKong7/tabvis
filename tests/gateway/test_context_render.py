"""Rendering a Context Pack into a system-prompt block (design §11 → model call path)."""

from __future__ import annotations

from tabvis.gateway.runtime.context.render import DEFAULT_EXCLUDED, render_system_context
from tabvis.gateway.runtime.context.request import ContextRequest
from tabvis.gateway.runtime.context.runtime import ContextRuntime


def _pack(**sources):
    req = ContextRequest(run_id="run_1", session_id="ses_1", model="m", sources=sources)
    return ContextRuntime().build(req)


def test_render_includes_situational_excludes_base_and_secrets() -> None:
    pack = _pack(
        project_instructions={"text": "USE CONVENTIONS", "ref": "TABVIS.md"},
        memory="REMEMBER THIS",
        workspace={"branch": "main", "status": "clean"},
        browser_snapshot={"url": "https://ex.com", "title": "Ex"},
        browser_credential_ref="secret://cookies",
    )
    block = render_system_context(pack)
    assert block is not None
    # situational sources are in...
    assert "main" in block and "https://ex.com" in block
    # ...base-prompt sources are excluded (avoid duplication)...
    assert "USE CONVENTIONS" not in block and "REMEMBER THIS" not in block
    # ...safety is excluded, and no secret material leaks.
    assert "secret://cookies" not in block
    # header carries the pack id + a short digest for traceability to explain().
    assert pack.context_pack_id in block and pack.digest[:12] in block


def test_render_returns_none_when_nothing_situational() -> None:
    # only base-prompt sources → nothing left to inject.
    pack = _pack(project_instructions={"text": "x", "ref": "TABVIS.md"}, memory="y")
    assert render_system_context(pack) is None


def test_render_respects_custom_exclusions() -> None:
    pack = _pack(workspace={"branch": "main"})
    # extend the default exclusions with workspace → nothing situational left to inject.
    assert render_system_context(pack, exclude_providers=DEFAULT_EXCLUDED | {"workspace"}) is None
