"""Rendering a Context Pack into a system-prompt block (design §11 → model call path).

Post base-prompt migration: the pack is authoritative for project instructions + memory + situational
context; only safety/agent stay the base prompt's job (excluded here). Secrets never render.
"""

from __future__ import annotations

from tabvis.gateway.runtime.context.render import DEFAULT_EXCLUDED, render_system_context
from tabvis.gateway.runtime.context.request import ContextRequest
from tabvis.gateway.runtime.context.runtime import ContextRuntime


def _pack(**sources):
    req = ContextRequest(run_id="run_1", session_id="ses_1", model="m", sources=sources)
    return ContextRuntime().build(req)


def test_render_includes_pack_context_excludes_safety_and_secrets() -> None:
    pack = _pack(
        project_instructions={"text": "USE CONVENTIONS", "ref": "TABVIS.md"},
        memory="REMEMBER THIS",
        workspace={"branch": "main", "status": "clean"},
        browser_snapshot={"url": "https://ex.com", "title": "Ex"},
        browser_credential_ref="secret://cookies",
    )
    block = render_system_context(pack)
    assert block is not None
    # the pack now owns project instructions + memory + situational context...
    assert "USE CONVENTIONS" in block and "REMEMBER THIS" in block
    assert "main" in block and "https://ex.com" in block
    # ...safety stays the base prompt's job (excluded)...
    assert "browser-driving coding agent" not in block  # the safety section text
    # ...and no secret material leaks.
    assert "secret://cookies" not in block
    # header carries the pack id + short digest for traceability to explain().
    assert pack.context_pack_id in block and pack.digest[:12] in block


def test_render_returns_none_when_only_base_owned_sections() -> None:
    # an otherwise-empty pack has only the (excluded) safety section → nothing to inject.
    assert render_system_context(_pack()) is None


def test_render_respects_custom_exclusions() -> None:
    pack = _pack(workspace={"branch": "main"})
    assert render_system_context(pack, exclude_providers=DEFAULT_EXCLUDED | {"workspace"}) is None
