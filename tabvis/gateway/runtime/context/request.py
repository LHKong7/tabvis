"""ContextRequest — the immutable input the runtime builds from (design §11.4, §11.6).

Providers read *only* from this request's ``sources`` snapshot, never from ambient state — that is what
makes a build reproducible and its digest stable. ``sources`` is a plain dict of already-collected
inputs (project instructions text, transcript messages, git state, browser snapshot, …); wiring those
from the live subsystems is an adapter concern kept out of the deterministic core.

The cache key (design §11.6) is derived from the fields a pack's identity depends on — model, session
head, workspace/browser revisions, and the enabled capability set — so a version is bumped only when
one of those actually changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextRequest:
    run_id: str
    session_id: str
    model: str
    max_tokens: int = 8000
    session_head: str = ""
    workspace_revision: str = ""
    browser_revision: str = ""
    capabilities: tuple[str, ...] = ()
    sources: dict[str, Any] = field(default_factory=dict)

    def cache_key(self) -> str:
        caps = ",".join(sorted(self.capabilities))
        return "|".join(
            [self.model, self.session_id, self.session_head, self.workspace_revision, self.browser_revision, caps]
        )
