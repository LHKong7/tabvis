"""ContextRuntime — build an immutable Context Pack and explain it (design §11.4, §11.6, §11.7).

``build`` runs the providers in fixed order, applies the deterministic budget, and assembles a pack
whose ``digest`` depends only on model + included source content — so identical sources hash
identically (design §15 acceptance). A version is assigned per cache key: unchanged sources keep the
version, changed sources bump it (design §11.6).

``explain`` returns a redacted report: every section's provenance and its include/drop reason, with
sensitive/secret content masked (design §11.7) — which is what makes budget decisions inspectable.
"""

from __future__ import annotations

from typing import Any

from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime.context import budget as budget_mod
from tabvis.gateway.runtime.context.pack import (
    KIND_MESSAGE,
    KIND_RESOURCE,
    KIND_SYSTEM,
    KIND_TOOL,
    ContextPack,
    ContextSection,
    ContextSource,
    compute_digest,
)
from tabvis.gateway.runtime.context.providers import ContextProvider, default_providers
from tabvis.gateway.runtime.context.redaction import redact_for_display
from tabvis.gateway.runtime.context.request import ContextRequest


class ContextRuntime:
    def __init__(self, providers: list[ContextProvider] | None = None) -> None:
        self._providers = providers if providers is not None else default_providers()
        # version bookkeeping (design §11.6) and a pack store for explain().
        self._versions: dict[str, dict[str, int]] = {}   # cache_key -> {digest: version}
        self._packs: dict[str, ContextPack] = {}

    # --- build ----------------------------------------------------------------------------------

    def build(self, request: ContextRequest) -> ContextPack:
        sections: list[ContextSection] = []
        for provider in self._providers:  # fixed §11.3 order
            sections.extend(provider.collect(request))

        decision = budget_mod.plan(sections, request.max_tokens)
        included_ids = {id(s) for s in decision.included}
        digest = compute_digest(request.model, decision.included)
        version = self._resolve_version(request.cache_key(), digest)

        pack = ContextPack(
            context_pack_id=ids.new_context_pack_id(),
            version=version,
            run_id=request.run_id,
            session_id=request.session_id,
            model=request.model,
            digest=digest,
            token_estimate=decision.total_tokens,
        )
        for section in decision.included:
            self._route(pack, section)

        # Provenance covers every section, included or dropped, with the reason (design §11.2).
        drop_reason = {id(s): reason for s, reason in decision.dropped}
        for section in sections:
            included = id(section) in included_ids
            pack.provenance.append(
                ContextSource(
                    provider_id=section.provider_id, key=section.key, kind=section.kind,
                    source_ref=section.source_ref, content_digest=section.content_digest(),
                    sensitivity=section.sensitivity, token_estimate=section.tokens(),
                    priority=section.priority, included=included,
                    dropped_reason=None if included else drop_reason.get(id(section), "budget"),
                ).to_dict()
            )

        self._packs[pack.context_pack_id] = pack
        return pack

    def _route(self, pack: ContextPack, section: ContextSection) -> None:
        entry = {
            "provider_id": section.provider_id,
            "title": section.title,
            "content": section.pack_content(),  # secret_ref → ref only (design §11.7)
            "sensitivity": section.sensitivity,
            "token_estimate": section.tokens(),
            "source_ref": section.source_ref,
        }
        if section.kind == KIND_MESSAGE:
            pack.messages.append(entry)
        elif section.kind == KIND_TOOL:
            pack.tool_descriptors.append(entry)
        elif section.kind == KIND_RESOURCE:
            pack.resource_refs.append(entry)
        else:  # KIND_SYSTEM
            pack.system_sections.append(entry)

    def _resolve_version(self, cache_key: str, digest: str) -> int:
        seen = self._versions.setdefault(cache_key, {})
        if digest in seen:
            return seen[digest]          # unchanged sources → same version
        version = max(seen.values(), default=0) + 1
        seen[digest] = version
        return version

    # --- explain --------------------------------------------------------------------------------

    def explain(self, context_pack_id: str) -> dict[str, Any]:
        """A redacted provenance report (design §11.4, §11.7). No secret/sensitive content leaks."""
        pack = self._packs.get(context_pack_id)
        if pack is None:
            raise GatewayError("NOT_FOUND", message="No such context pack", details={"context_pack_id": context_pack_id})

        # Index the assembled content so the report can show a redacted preview.
        content_by_ref: dict[tuple[str, str], str] = {}
        for bucket in (pack.system_sections, pack.messages, pack.tool_descriptors, pack.resource_refs):
            for e in bucket:
                content_by_ref[(e["provider_id"], e["source_ref"])] = e["content"]

        sections_report = []
        for p in pack.provenance:
            content = content_by_ref.get((p["provider_id"], p["source_ref"]), "")
            sections_report.append({
                "provider_id": p["provider_id"],
                "key": p["key"],
                "kind": p["kind"],
                "source_ref": p["source_ref"],
                "sensitivity": p["sensitivity"],
                "priority": p["priority"],
                "token_estimate": p["token_estimate"],
                "included": p["included"],
                "dropped_reason": p["dropped_reason"],
                "content_preview": redact_for_display(p["sensitivity"], content) if p["included"] else None,
            })

        return {
            "context_pack_id": pack.context_pack_id,
            "version": pack.version,
            "model": pack.model,
            "digest": pack.digest,
            "token_estimate": pack.token_estimate,
            "sections": sections_report,
        }


_runtime: ContextRuntime | None = None


def get_context_runtime() -> ContextRuntime:
    global _runtime
    if _runtime is None:
        _runtime = ContextRuntime()
    return _runtime
