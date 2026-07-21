"""Live source collection for the Context Runtime (design §11.3).

The Context Runtime core is a pure function of its `ContextRequest.sources` snapshot — that is what
makes a build reproducible. This adapter is where the snapshot comes *from*: it gathers the real tabvis
subsystems (project `TABVIS.md` instructions, project memory, Git/workspace state, the browser summary,
plus caller-supplied transcript/tools/skills) into that snapshot, then hands it to the runtime.

Two properties keep it honest:

* **Graceful degradation** — every source is gathered under a guard; a subsystem that errors or is
  absent simply omits its section, it never breaks context assembly (design §11.3: providers degrade
  independently).
* **Injectable** — each source is a hook whose default is the real loader, so the collector is
  unit-tested with fakes and the deterministic core stays untouched.

Wiring the transcript's conversation-chain format into the provider's simple message shape, and feeding
the assembled pack back into the model call path, are follow-ups; this brings the live sources in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tabvis.gateway.runtime.context.pack import ContextPack
from tabvis.gateway.runtime.context.request import ContextRequest
from tabvis.gateway.runtime.context.runtime import ContextRuntime, get_context_runtime
from tabvis.utils.debug import log_for_debugging

AsyncStr = Callable[[], Awaitable["str | None"]]
TranscriptLoader = Callable[[str], Awaitable[list]]
BrowserSummary = Callable[[str], "dict | None"]


async def _real_project_instructions() -> str | None:
    from tabvis.agent.project_instructions import load_project_instructions_prompt

    return await load_project_instructions_prompt()


async def _real_memory() -> str | None:
    from tabvis.agent.mem.memdir import load_memory_prompt

    return await load_memory_prompt()


async def _real_git_status() -> str | None:
    from tabvis.agent.context import _get_git_status

    return await _get_git_status()


def _real_browser_summary(agent_id: str) -> dict | None:
    from tabvis.browser.manager import get_session_summary

    summary = get_session_summary(agent_id)
    return summary or None


@dataclass
class SourceCollector:
    """Gathers a live :class:`ContextRequest`. Each field overrides its real loader (tests inject fakes)."""

    project_instructions: AsyncStr | None = None
    memory: AsyncStr | None = None
    git_status: AsyncStr | None = None
    browser_summary: BrowserSummary | None = None
    transcript: TranscriptLoader | None = None     # session_id -> [{id, role, text}]; default: none
    compact_summaries: list | None = None
    tool_descriptors: list | None = None
    mcp_resources: list | None = None
    skills: list | None = None
    channel_identity: dict | None = None
    agent_definition: str | None = None
    browser_credential_ref: str | None = None

    async def collect(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str = "",
        model: str = "",
        max_tokens: int = 8000,
        capabilities: tuple[str, ...] = (),
        workspace_revision: str = "",
        browser_revision: str = "",
    ) -> ContextRequest:
        sources: dict[str, Any] = {}

        _set(sources, "agent_definition", self.agent_definition)

        pi = await _guard_async(self.project_instructions or _real_project_instructions)
        if pi:
            sources["project_instructions"] = {"text": pi, "ref": "TABVIS.md"}

        mem = await _guard_async(self.memory or _real_memory)
        if mem:
            sources["memory"] = mem

        git = await _guard_async(self.git_status or _real_git_status)
        if git:
            sources["workspace"] = git

        if session_id and self.transcript is not None:
            tx = await _guard_async(lambda: self.transcript(session_id))
            if tx:
                sources["transcript"] = tx

        if agent_id:
            bs = _guard_sync(lambda: (self.browser_summary or _real_browser_summary)(agent_id))
            if bs:
                sources["browser_snapshot"] = bs

        _set(sources, "compact_summaries", self.compact_summaries)
        _set(sources, "tool_descriptors", self.tool_descriptors)
        _set(sources, "mcp_resources", self.mcp_resources)
        _set(sources, "skills", self.skills)
        _set(sources, "channel_identity", self.channel_identity)
        _set(sources, "browser_credential_ref", self.browser_credential_ref)

        return ContextRequest(
            run_id=run_id, session_id=session_id, model=model, max_tokens=max_tokens,
            capabilities=tuple(capabilities), workspace_revision=workspace_revision,
            browser_revision=browser_revision, sources=sources,
        )

    async def build_pack(self, *, runtime: ContextRuntime | None = None, **kwargs: Any) -> ContextPack:
        """Collect live sources and assemble a Context Pack in one call."""
        request = await self.collect(**kwargs)
        return (runtime or get_context_runtime()).build(request)


def _set(sources: dict, key: str, value: Any) -> None:
    if value:
        sources[key] = value


async def _guard_async(fn: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await fn()
    except Exception as e:  # noqa: BLE001 - a failing source degrades to omitted, never fatal
        log_for_debugging(f"[CONTEXT] source failed: {e}")
        return None


def _guard_sync(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[CONTEXT] source failed: {e}")
        return None
