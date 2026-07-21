"""Browser Runtime contracts (design §10.4, §10.6).

The binding-based interface (design §10.4) and the capability contracts for tabs/DOM/network/storage/
downloads (design §10.6). The runtime hands agent tools a :class:`BrowserBinding` (an id + metadata),
never a live browser; real operations go through the :class:`BrowserDriver` seam, which a deployment
implements over Playwright (or a fake, in tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class BrowserAcquireRequest:
    agent_id: str
    run_id: str
    profile: str | None = None   # a shared named profile, or None for the agent's isolated profile
    engine: str = "chromium"


@dataclass
class BrowserBinding:
    """What an agent tool is given (design §10.4): an id and metadata, never a raw browser."""

    binding_id: str
    identity_id: str
    profile_key: str
    agent_id: str
    run_id: str
    engine: str
    expires_at: str


@dataclass
class Tab:
    """A stable-id tab (design §10.6)."""

    tab_id: str
    url: str = "about:blank"
    title: str = ""
    active: bool = False
    opened_at: str = ""
    closed_at: str | None = None


@dataclass
class ArtifactRef:
    """A content-addressed artifact reference — never inline bytes in events (design §10.6)."""

    artifact_id: str
    type: str            # dom | screenshot | download | network
    digest: str          # content address (sha256) or file digest
    ref: str             # where the bytes live (blob store key / quarantined path)
    url: str | None = None
    size_bytes: int = 0
    truncated: bool = False


@dataclass
class NetworkObservation:
    """Network metadata only; bodies are off by default (design §10.6)."""

    method: str
    url: str
    status: int | None = None
    include_body: bool = False


@dataclass
class BrowserSnapshot:
    binding_id: str
    session_state: str
    tabs: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    current_url: str | None = None


@dataclass
class BrowserIntent:
    """An instruction to the driver (navigate/click/screenshot/download). Side-effecting flag drives
    the interrupted-on-uncertainty recovery rule (design §10.7)."""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    side_effecting: bool = False


@dataclass
class ExecutionRecord:
    intent: str
    status: str          # succeeded | failed | interrupted
    tab_id: str | None = None
    artifact: ArtifactRef | None = None
    detail: str | None = None


class BrowserDriver(Protocol):
    """The real-browser seam. A deployment implements this over Playwright; tests use a fake."""

    async def launch(self, profile_key: str, engine: str) -> None: ...
    async def execute(self, profile_key: str, intent: BrowserIntent) -> dict[str, Any]: ...
    async def verify_identity(self, profile_key: str) -> bool: ...
    async def close(self, profile_key: str) -> None: ...
