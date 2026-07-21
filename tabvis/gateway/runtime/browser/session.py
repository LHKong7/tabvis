"""Browser session — state machine, stable tabs, content-addressed artifacts (design §10.3, §10.6).

A :class:`BrowserSession` tracks the §10.3 lifecycle and the observable workspace: tabs with stable
ids, and artifacts stored **by content address** so an event carries a reference, never inline bytes or
base64 (design §10.6). DOM snapshots are size-limited and truncation is flagged rather than silently
dropping content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime.browser.contracts import ArtifactRef, Tab

# §10.3 states
REQUESTED: Final = "requested"
LAUNCHING: Final = "launching"
READY: Final = "ready"
BUSY: Final = "busy"
DISCONNECTED: Final = "disconnected"
CLOSING: Final = "closing"
CLOSED: Final = "closed"
FAILED: Final = "failed"

_TRANSITIONS: dict[str, frozenset[str]] = {
    REQUESTED: frozenset({LAUNCHING, FAILED}),
    LAUNCHING: frozenset({READY, FAILED}),
    READY: frozenset({BUSY, CLOSING}),
    BUSY: frozenset({READY, DISCONNECTED, CLOSING}),
    DISCONNECTED: frozenset({BUSY, FAILED}),
    CLOSING: frozenset({CLOSED}),
    CLOSED: frozenset(),
    FAILED: frozenset(),
}

MAX_DOM_BYTES: Final = 256 * 1024  # content-addressed DOM snapshots are capped (design §10.6)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArtifactStore:
    """A minimal content-addressed blob store. The ref, not the bytes, travels in events (§10.6)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, kind: str, content: bytes, *, url: str | None = None, max_bytes: int = MAX_DOM_BYTES) -> ArtifactRef:
        truncated = len(content) > max_bytes
        stored = content[:max_bytes] if truncated else content
        digest = hashlib.sha256(stored).hexdigest()
        self._blobs.setdefault(digest, stored)
        return ArtifactRef(
            artifact_id=ids.new_workspace_id().replace("ws_", "art_"),
            type=kind, digest=digest, ref=f"blob:{digest}", url=url,
            size_bytes=len(stored), truncated=truncated,
        )

    def get(self, digest: str) -> bytes | None:
        return self._blobs.get(digest)


@dataclass
class BrowserSession:
    session_id: str
    binding_id: str
    profile_key: str
    state: str = REQUESTED
    tabs: dict[str, Tab] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    downloads: set[str] = field(default_factory=set)
    active_tab_id: str | None = None

    def transition(self, to_state: str) -> None:
        if to_state not in _TRANSITIONS.get(self.state, frozenset()):
            raise GatewayError(
                "CONFLICT",
                message=f"Browser session cannot transition {self.state!r} → {to_state!r}",
                details={"from": self.state, "to": to_state},
            )
        self.state = to_state

    def open_tab(self, url: str = "about:blank", title: str = "") -> Tab:
        tab = Tab(tab_id=ids.new_workspace_id().replace("ws_", "tab_"), url=url, title=title,
                  active=True, opened_at=_utc_now())
        for other in self.tabs.values():
            other.active = False
        self.tabs[tab.tab_id] = tab
        self.active_tab_id = tab.tab_id
        return tab

    def navigate(self, tab_id: str, url: str, title: str = "") -> Tab:
        tab = self.tabs.get(tab_id)
        if tab is None:
            raise GatewayError("NOT_FOUND", message="Unknown tab", details={"tab_id": tab_id})
        tab.url, tab.title = url, title
        return tab

    def close_tab(self, tab_id: str) -> None:
        tab = self.tabs.get(tab_id)
        if tab is not None:
            tab.closed_at = _utc_now()
            tab.active = False
            if self.active_tab_id == tab_id:
                self.active_tab_id = None

    def quarantine_name(self, name: str) -> str:
        """A collision-safe download name in the session's quarantine (design §10.6)."""
        candidate, i = name, 1
        while candidate in self.downloads:
            stem, dot, ext = name.partition(".")
            candidate = f"{stem}-{i}{dot}{ext}"
            i += 1
        self.downloads.add(candidate)
        return f"quarantine/{self.session_id}/{candidate}"

    def add_artifact(self, artifact: ArtifactRef) -> None:
        self.artifacts.append(artifact)

    def snapshot_dict(self) -> dict:
        return {
            "session_state": self.state,
            "tabs": [t.__dict__ for t in self.tabs.values()],
            "artifacts": [a.__dict__ for a in self.artifacts],
            "current_url": self.tabs[self.active_tab_id].url if self.active_tab_id else None,
        }
