"""Post-compaction cleanup.

Runs cleanup of caches and tracking state after compaction. Call this after
both auto-compact and manual ``/compact`` to free memory held by tracking
structures invalidated by compaction.

Cycle note: ``micro_compact`` (``reset_microcompact_state``) is a member of the
compact import cycle, so it is imported lazily inside the function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # type-only
    from tabvis.constants.query_source import QuerySource


def run_post_compact_cleanup(query_source: QuerySource | None = None) -> None:
    """Run cleanup of caches and tracking state after compaction.

    ``query_source`` remains in the public signature because compaction callers pass it; the
    current cleanup state is safe to reset for every source.

    Note: we intentionally do NOT clear invoked skill content here. Skill content
    must survive across multiple compactions so that
    ``create_skill_attachment_if_needed`` can include the full skill text in
    subsequent compaction attachments.
    """
    # Lazy: micro_compact is in the compact cycle.
    from tabvis.agent.compact.micro_compact import reset_microcompact_state

    reset_microcompact_state()

    _clear_system_prompt_sections()
    # Intentionally NOT calling reset_sent_skill_names(): re-injecting the full
    # skill_listing (~4K tokens) post-compact is pure cache_creation. See
    # compact_conversation() for full rationale.

    from tabvis.utils.session_storage import clear_session_messages_cache

    clear_session_messages_cache()


def _clear_system_prompt_sections() -> None:
    """Clear the cached system-prompt sections.

    The ``clear_system_prompt_sections`` helper may not be available yet; degrade
    gracefully so this cleanup never breaks the compaction path.
    """
    try:
        from tabvis.constants.system_prompt_sections import (  # type: ignore[attr-defined]
            clear_system_prompt_sections,
        )
    except (ImportError, AttributeError):
        return
    clear_system_prompt_sections()
