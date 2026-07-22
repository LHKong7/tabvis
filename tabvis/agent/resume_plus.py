"""Resume Plus — the resolver and public domain types (design §4, §5, §12).

Resume Plus lets a returning agent continue an existing conversation *and* the same browser identity
instead of starting blank. This module is the entry point Phase 1 delivers: the strict resolver that
turns a ``(principal, session_id)`` selector into an immutable :class:`ResumeTarget`, plus the mode
enum, stable error codes, and a fresh ``run_id`` for the new execution.

The resolver **fails closed** (§4.4): it never invents a fresh session, never guesses between two
agents, and validates the selector before it touches the filesystem. It resolves through two sources,
in order:

1. the durable **agent registry** — a server/gateway agent records its ``session_id``, ``profile``,
   ``cwd`` and owning ``principal_id``; a reverse lookup recovers exactly one agent (or reports
   ``RESUME_SESSION_AMBIGUOUS`` for >1);
2. the **transcript on disk** — a one-shot CLI session has no durable agent record, so its
   ``<session_id>.jsonl`` is located across project dirs to recover the original project directory
   (the resume then uses the CLI's default agent/profile).

Higher layers (Phase 2+) enforce memory consent, resident-browser leases, and live attach. This
module deliberately does **not** launch a browser or read Agent Memory — it only resolves *where* the
Run should read/write and *which* recovery is possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from tabvis.agent.agents import registry
from tabvis.agent.agents.registry import LOCAL_PRINCIPAL, new_run_id

ResumeMode = Literal["fresh", "conversation_only", "plus"]

# Recovery states reported back to the caller (§5.2). Phase 1 only ever *reports* cold recovery for a
# one-shot CLI (``relaunched_profile``) or ``unavailable``; live attach is a later phase.
BrowserRecovery = Literal[
    "attached_live", "relaunched_profile", "remote_attached", "unavailable", "new_profile"
]


class ResumeErrorCode:
    """Stable error codes for the Resume surface (§12.4)."""

    SESSION_NOT_FOUND = "RESUME_SESSION_NOT_FOUND"
    SESSION_AMBIGUOUS = "RESUME_SESSION_AMBIGUOUS"
    IDENTITY_MISMATCH = "RESUME_IDENTITY_MISMATCH"
    FORBIDDEN = "RESUME_FORBIDDEN"
    BROWSER_PROFILE_MISSING = "BROWSER_PROFILE_MISSING"
    AGENT_RUN_ACTIVE = "AGENT_RUN_ACTIVE"
    INVALID_SELECTOR = "RESUME_INVALID_SELECTOR"


class ResumeError(Exception):
    """A Resume request refused by the resolver. ``code`` is one of :class:`ResumeErrorCode`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResumeTarget:
    """The fully-resolved plan for one Resume execution (§12.1).

    Immutable and self-contained: ``run_headless`` / ``stream_agent`` receive this rather than
    re-deriving identifiers. ``project_dir`` is the *resolved* transcript project (so a cross-project
    resume finds its history), and ``browser_recovery`` states honestly what recovery is possible.
    """

    mode: ResumeMode
    principal_id: str
    agent_id: str
    session_id: str
    run_id: str
    cwd: str
    project_dir: str | None
    profile: str | None
    browser_recovery: BrowserRecovery
    read_memory: bool
    write_memory: bool
    allow_new_browser: bool = False
    warnings: tuple[str, ...] = ()


@dataclass
class ResumeResult:
    """What a Resume actually restored, for the caller/UX to report (§5.2)."""

    resume_mode: ResumeMode
    agent_id: str
    session_id: str
    run_id: str
    browser_recovery: BrowserRecovery
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "resumeMode": self.resume_mode,
            "agentId": self.agent_id,
            "sessionId": self.session_id,
            "runId": self.run_id,
            "browserRecovery": self.browser_recovery,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
        }


def resolve_resume(
    session_id: str,
    *,
    mode: ResumeMode = "plus",
    principal_id: str = LOCAL_PRINCIPAL,
    current_cwd: str,
    allow_new_browser: bool = False,
    read_memory: bool = True,
    write_memory: bool = True,
    allow_cwd_change: bool = False,
    resident: bool = False,
) -> ResumeTarget:
    """Resolve a ``(principal, session_id)`` selector to an immutable :class:`ResumeTarget`.

    ``resident`` reports whether the caller owns a live daemon workspace: a one-shot CLI passes
    ``False`` and can only ever offer a cold ``relaunched_profile`` recovery — it must never claim the
    browser was resident (§6.1). Raises :class:`ResumeError` on any fail-closed condition.
    """
    from tabvis.utils.session_storage import find_session_project_dir, is_safe_session_id

    session_id = (session_id or "").strip()
    if not is_safe_session_id(session_id):
        raise ResumeError(
            ResumeErrorCode.INVALID_SELECTOR,
            f"invalid session id {session_id!r}: expected a plain session token.",
        )

    warnings: list[str] = []
    matches = registry.find_agents_by_session(session_id, principal_id=principal_id)

    # Guard against a leaked cross-principal claim before revealing anything else (§4.4): if some
    # agent holds the session but under a different owner, it is forbidden, not "not found".
    if not matches:
        any_owner = registry.find_agents_by_session(session_id)
        if any_owner:
            raise ResumeError(
                ResumeErrorCode.FORBIDDEN,
                "the requested session is owned by a different principal.",
            )

    if len(matches) > 1:
        raise ResumeError(
            ResumeErrorCode.SESSION_AMBIGUOUS,
            f"session {session_id} is claimed by {len(matches)} agents; refusing to guess.",
        )

    if matches:
        agent = matches[0]
        # Single-active-Run guard (§16.1): do not double-drive an agent already running.
        if registry.active_run(agent.agent_id) is not None:
            raise ResumeError(
                ResumeErrorCode.AGENT_RUN_ACTIVE,
                f"agent {agent.agent_id} already has an active run.",
            )
        resolved_cwd = agent.cwd or current_cwd
        if agent.cwd and agent.cwd != current_cwd and not allow_cwd_change:
            # cwd selects project instructions and tool scope — a silent change is a footgun (§4.4).
            raise ResumeError(
                ResumeErrorCode.IDENTITY_MISMATCH,
                f"session was created under cwd {agent.cwd!r}, but the current cwd is "
                f"{current_cwd!r}. Re-run from that directory (a cwd-change fork is a later option).",
            )
        project_dir = find_session_project_dir(session_id)
        recovery = _recovery_for(resident, agent.profile, project_dir, allow_new_browser)
        return ResumeTarget(
            mode=mode,
            principal_id=principal_id,
            agent_id=agent.agent_id,
            session_id=session_id,
            run_id=new_run_id(),
            cwd=resolved_cwd,
            project_dir=project_dir,
            profile=agent.profile,
            browser_recovery=recovery,
            read_memory=read_memory and mode == "plus",
            write_memory=write_memory and mode == "plus",
            allow_new_browser=allow_new_browser,
            warnings=tuple(warnings),
        )

    # No durable agent — resolve a one-shot/CLI session by its transcript on disk.
    from tabvis.browser.manager import DEFAULT_AGENT_ID, DEFAULT_PROFILE

    project_dir = find_session_project_dir(session_id)
    if project_dir is None:
        raise ResumeError(
            ResumeErrorCode.SESSION_NOT_FOUND,
            f"no session {session_id} found for this principal.",
        )
    return ResumeTarget(
        mode=mode,
        principal_id=principal_id,
        agent_id=DEFAULT_AGENT_ID,
        session_id=session_id,
        run_id=new_run_id(),
        cwd=current_cwd,
        project_dir=project_dir,
        profile=DEFAULT_PROFILE,
        # A one-shot CLI is never resident: the most it can offer is a cold profile relaunch.
        browser_recovery="relaunched_profile" if not resident else "attached_live",
        read_memory=read_memory and mode == "plus",
        write_memory=write_memory and mode == "plus",
        allow_new_browser=allow_new_browser,
        warnings=tuple(warnings),
    )


def _recovery_for(
    resident: bool, profile: str | None, project_dir: str | None, allow_new_browser: bool
) -> BrowserRecovery:
    """Decide the honest recovery mode for a resolved agent (§5.2/§6.1)."""
    if resident:
        return "attached_live"
    return "relaunched_profile"
