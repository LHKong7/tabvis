"""Context Pack data model, sensitivity labels, and digest (design §11.2, §11.6, §11.7).

A :class:`ContextSection` is one labeled contribution from a provider; a :class:`ContextPack` is the
immutable assembled result the model consumes. Two rules are enforced structurally here:

* **No secret enters the pack** (design §11.7): a ``secret_ref`` section's stored content is its
  reference only — :func:`ContextPack.section_content` drops any provider-supplied value.
* **Reproducible digest** (design §11.6): the digest is a hash of the model plus the ordered included
  sections' *content digests* (source-ref digest for secret refs), independent of the volatile
  ``context_pack_id`` and timestamps — so identical source revisions hash identically.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

# sensitivity labels (design §11.7)
PUBLIC: Final = "public"
WORKSPACE: Final = "workspace"
SENSITIVE: Final = "sensitive"
SECRET_REF: Final = "secret_ref"

# section kinds — how a section is routed into the pack (design §11.2)
KIND_SYSTEM: Final = "system"
KIND_MESSAGE: Final = "message"
KIND_TOOL: Final = "tool"
KIND_RESOURCE: Final = "resource"

_REDACTED_IN_PACK = {SECRET_REF}  # never stored as content; only the ref survives


def estimate_tokens(text: str) -> int:
    """A deterministic, model-agnostic token estimate (~4 chars/token). Stable across runs."""
    return max(1, (len(text) + 3) // 4)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ContextSection:
    """One provider contribution (design §11.3: content, priority, sensitivity, tokens, cache, provenance)."""

    provider_id: str
    key: str
    kind: str
    title: str
    content: str
    priority: int = 50          # 0..100, higher = more important to keep under budget
    sensitivity: str = PUBLIC
    cache_scope: str = "run"    # run | session | workspace | static
    source_ref: str = ""        # provenance: what this came from, e.g. "TABVIS.md@rev7"
    required: bool = False       # reserved first, never dropped by the budget (design §11.5)
    freshness: int = 0           # higher = fresher; a budget tiebreak
    token_estimate: int | None = None

    def tokens(self) -> int:
        return self.token_estimate if self.token_estimate is not None else estimate_tokens(self.content)

    @property
    def section_id(self) -> str:
        return f"{self.provider_id}:{self.key}"

    def content_digest(self) -> str:
        # For a secret ref, hash the reference, never the (volatile, secret) value.
        return _sha(self.source_ref if self.sensitivity in _REDACTED_IN_PACK else self.content)

    def pack_content(self) -> str:
        """The content actually stored in the pack — the ref only for a secret_ref (design §11.7)."""
        return self.source_ref if self.sensitivity in _REDACTED_IN_PACK else self.content


@dataclass
class ContextSource:
    """A provenance entry: what a section was, and whether/why it made the cut (design §11.2)."""

    provider_id: str
    key: str
    kind: str
    source_ref: str
    content_digest: str
    sensitivity: str
    token_estimate: int
    priority: int
    included: bool
    dropped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContextPack:
    """The immutable assembled context (design §11.2)."""

    context_pack_id: str
    version: int
    run_id: str
    session_id: str
    model: str
    digest: str
    token_estimate: int
    system_sections: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_descriptors: list[dict[str, Any]] = field(default_factory=list)
    resource_refs: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_digest(model: str, included_ordered: list[ContextSection]) -> str:
    """Reproducible pack digest: model + ordered section content digests (design §11.6)."""
    parts = [f"model={model}"]
    parts.extend(f"{s.section_id}={s.content_digest()}" for s in included_ordered)
    return _sha("\n".join(parts))
