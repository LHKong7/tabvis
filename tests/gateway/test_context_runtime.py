"""Phase 5 — Context Runtime: determinism, budget, provenance, redaction (design §11, §15)."""

from __future__ import annotations

from tabvis.gateway.runtime.context.pack import SECRET_REF, SENSITIVE
from tabvis.gateway.runtime.context.request import ContextRequest
from tabvis.gateway.runtime.context.runtime import ContextRuntime


def _request(**overrides) -> ContextRequest:
    sources = {
        "agent_definition": "You are a research agent.",
        "project_instructions": {"text": "Use TABVIS conventions.", "ref": "TABVIS.md@rev7"},
        "transcript": [
            {"id": "m1", "role": "user", "text": "hello"},
            {"id": "m2", "role": "assistant", "text": "hi"},
            {"id": "m3", "role": "user", "text": "inspect the page"},
        ],
        "compact_summaries": [{"text": "earlier: set up the project"}],
        "workspace": {"branch": "main", "status": "clean"},
        "browser_snapshot": {"url": "https://example.com", "title": "Example"},
        "browser_credential_ref": "secret://browser/cookies",
        "memory": "prefers concise answers",
        "todos": ["open page", "read title"],
        "channel_identity": {"channel": "web", "user": "u1"},
        "tool_descriptors": [{"name": "click", "schema": {"type": "object"}}],
    }
    base = dict(
        run_id="run_1", session_id="ses_1", model="claude-x", max_tokens=8000,
        workspace_revision="wr1", browser_revision="br1", capabilities=("browser",), sources=sources,
    )
    base.update(overrides)
    return ContextRequest(**base)


def test_providers_run_in_fixed_order() -> None:
    pack = ContextRuntime().build(_request())
    # safety is always first among system sections (design §11.3 #1).
    assert pack.system_sections[0]["provider_id"] == "safety"
    provider_order = [s["provider_id"] for s in pack.system_sections]
    assert provider_order.index("safety") < provider_order.index("project_instructions")
    assert provider_order.index("agent") < provider_order.index("workspace")


def test_identical_sources_produce_identical_digest() -> None:
    # design §15 Phase 5 acceptance: identical source revisions produce the same digest.
    rt = ContextRuntime()
    a = rt.build(_request())
    b = rt.build(_request())
    assert a.digest == b.digest
    assert a.context_pack_id != b.context_pack_id  # id is volatile; digest is stable
    assert a.version == b.version == 1              # unchanged sources keep the version


def test_changed_source_bumps_the_version() -> None:
    rt = ContextRuntime()
    first = rt.build(_request())
    changed = _request()
    changed.sources["project_instructions"] = {"text": "NEW conventions.", "ref": "TABVIS.md@rev7"}
    second = rt.build(changed)  # same cache key, different content digest
    assert second.digest != first.digest
    assert second.version == 2


def test_different_cache_keys_version_independently() -> None:
    rt = ContextRuntime()
    a = rt.build(_request(model="model-a"))
    b = rt.build(_request(model="model-b"))
    assert a.version == 1 and b.version == 1  # separate cache keys each start at 1


def test_no_secret_material_enters_the_pack() -> None:
    # design §15 Phase 5 acceptance: no secret material appears in Context Pack snapshots.
    pack = ContextRuntime().build(_request())
    cred = [s for s in pack.system_sections if s["provider_id"] == "browser" and s["sensitivity"] == SECRET_REF]
    assert len(cred) == 1
    assert cred[0]["content"] == "secret://browser/cookies"  # the ref, not a value
    # the placeholder value the provider carried never made it into the pack.
    serialized = str(pack.to_dict())
    assert "<secret>" not in serialized


def test_current_user_message_is_reserved_and_present() -> None:
    pack = ContextRuntime().build(_request(max_tokens=60))  # tight budget
    texts = [m["content"] for m in pack.messages]
    assert any("inspect the page" in t for t in texts)  # the final user message survives (required)


def test_tool_schemas_are_reserved_even_under_tight_budget() -> None:
    pack = ContextRuntime().build(_request(max_tokens=1))
    assert any(t["provider_id"] == "tools" for t in pack.tool_descriptors)  # required, never dropped


def test_low_priority_sections_drop_under_budget_with_a_reason() -> None:
    pack = ContextRuntime().build(_request(max_tokens=80))
    dropped = [p for p in pack.provenance if not p["included"]]
    assert dropped, "expected some optional sections to be dropped under a tight budget"
    assert all(p["dropped_reason"] == "budget" for p in dropped)
    # nothing required was dropped.
    assert all(p["included"] for p in pack.provenance if p["priority"] == 100)


def test_dropped_section_is_entirely_absent_no_partial_truncation() -> None:
    pack = ContextRuntime().build(_request(max_tokens=80))
    included_refs = {
        (e["provider_id"], e["source_ref"])
        for bucket in (pack.system_sections, pack.messages, pack.tool_descriptors, pack.resource_refs)
        for e in bucket
    }
    for p in pack.provenance:
        if not p["included"]:
            assert (p["provider_id"], p["source_ref"]) not in included_refs  # whole-or-nothing


def test_explain_is_redacted_but_keeps_provenance() -> None:
    rt = ContextRuntime()
    pack = rt.build(_request())
    report = rt.explain(pack.context_pack_id)
    assert report["digest"] == pack.digest

    by_provider = {}
    for s in report["sections"]:
        by_provider.setdefault(s["provider_id"], []).append(s)

    # sensitive transcript messages are redacted in the report...
    msg = next(
        s for s in report["sections"]
        if s["provider_id"] == "transcript" and s["key"].startswith("msg-") and s["included"]
    )
    assert msg["sensitivity"] == SENSITIVE
    assert msg["content_preview"] == "[redacted:sensitive]"
    # ...secret refs too...
    cred = next(s for s in report["sections"] if s["sensitivity"] == SECRET_REF)
    assert cred["content_preview"] == "[redacted:secret_ref]"
    # ...but public safety content is shown, and provenance (source_ref) is always preserved.
    safety = next(s for s in report["sections"] if s["provider_id"] == "safety")
    assert "tabvis" in safety["content_preview"].lower()
    assert all(s["source_ref"] for s in report["sections"])


def test_explain_unknown_pack_is_not_found() -> None:
    import pytest

    from tabvis.gateway.protocol.errors import GatewayError

    with pytest.raises(GatewayError) as ei:
        ContextRuntime().explain("ctx_nope")
    assert ei.value.code == "NOT_FOUND"
