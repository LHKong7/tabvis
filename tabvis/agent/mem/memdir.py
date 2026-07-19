"""Memory prompt assembly.

``load_memory_prompt`` emits the big ``# auto memory`` block injected into the system
prompt. Team memory, the ``skip_index`` variant, the cowork extra-guidelines env var, and
analytics logging all default to their clean-env values (off / empty).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TypedDict

from tabvis.agent.mem.memory_types import (
    MEMORY_FRONTMATTER_EXAMPLE,
    TRUSTING_RECALL_SECTION,
    TYPES_SECTION_INDIVIDUAL,
    WHAT_NOT_TO_SAVE_SECTION,
    WHEN_TO_ACCESS_SECTION,
)
from tabvis.agent.mem.paths import get_auto_mem_path, is_auto_memory_enabled

ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 40_000


class TruncatedEntrypoint(TypedDict):
    content: str
    truncated: bool


DIR_EXISTS_GUIDANCE = (
    "This directory already exists — write to it directly with the Write tool "
    "(do not run mkdir or check for its existence)."
)


def ensure_memory_dir_exists(memory_dir: str) -> None:
    """Idempotently create the memory directory (recursive; swallows EEXIST)."""
    try:
        os.makedirs(memory_dir, exist_ok=True)
    except OSError:
        # A real perm error (EACCES/EPERM/EROFS). Prompt building continues; the
        # model's Write will surface the real error.
        pass


def truncate_entrypoint_content(content: str) -> TruncatedEntrypoint:
    """Limit ``MEMORY.md`` to the context-safe line and UTF-8 byte caps.

    Lines are limited first, then bytes. The byte slice is decoded with an incomplete trailing
    code point discarded, so truncation always returns valid UTF-8 text.
    """
    line_limited = "".join(content.splitlines(keepends=True)[:MAX_ENTRYPOINT_LINES])
    encoded = line_limited.encode("utf-8")
    byte_limited = (
        encoded[:MAX_ENTRYPOINT_BYTES].decode("utf-8", errors="ignore")
        if len(encoded) > MAX_ENTRYPOINT_BYTES
        else line_limited
    )
    return {
        "content": byte_limited,
        "truncated": byte_limited != content,
    }


async def _read_entrypoint(memory_dir: str) -> TruncatedEntrypoint:
    """Read and truncate the auto-memory index; a missing/unreadable index is empty memory."""
    entrypoint = Path(memory_dir, ENTRYPOINT_NAME)
    try:
        content = await asyncio.to_thread(entrypoint.read_text, encoding="utf-8")
    except (OSError, UnicodeError):
        return {"content": "", "truncated": False}
    return truncate_entrypoint_content(content)


def build_memory_lines(
    display_name: str,
    memory_dir: str,
    extra_guidelines: list[str] | None = None,
    skip_index: bool = False,
) -> list[str]:
    """Build the typed-memory behavioral instructions (without MEMORY.md content)."""
    if skip_index:
        how_to_save = [
            "## How to save memories",
            "",
            "Write each memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            "- Keep the name, description, and type fields in memory files up-to-date with the content",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]
    else:
        how_to_save = [
            "## How to save memories",
            "",
            "Saving a memory is a two-step process:",
            "",
            "**Step 1** — write the memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            f"**Step 2** — add a pointer to that file in `{ENTRYPOINT_NAME}`. `{ENTRYPOINT_NAME}` is an index, not a memory — each entry should be one line, under ~150 characters: `- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory content directly into `{ENTRYPOINT_NAME}`.",
            "",
            f"- `{ENTRYPOINT_NAME}` is always loaded into your conversation context — lines after {MAX_ENTRYPOINT_LINES} will be truncated, so keep the index concise",
            "- Keep the name, description, and type fields in memory files up-to-date with the content",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]

    lines: list[str] = [
        f"# {display_name}",
        "",
        f"You have a persistent, file-based memory system at `{memory_dir}`. {DIR_EXISTS_GUIDANCE}",
        "",
        "You should build up this memory system over time so that future conversations can have a complete picture of who the user is, how they'd like to collaborate with you, what behaviors to avoid or repeat, and the context behind the work the user gives you.",
        "",
        "If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.",
        "",
        *TYPES_SECTION_INDIVIDUAL,
        *WHAT_NOT_TO_SAVE_SECTION,
        "",
        *how_to_save,
        "",
        *WHEN_TO_ACCESS_SECTION,
        "",
        *TRUSTING_RECALL_SECTION,
        "",
        "## Memory and other forms of persistence",
        "Memory is one of several persistence mechanisms available to you as you assist the user in a given conversation. The distinction is often that memory can be recalled in future conversations and should not be used for persisting information that is only useful within the scope of the current conversation.",
        "- When to use or update a plan instead of memory: If you are about to start a non-trivial implementation task and would like to reach alignment with the user on your approach you should use a Plan rather than saving this information to memory. Similarly, if you already have a plan within the conversation and you have changed your approach persist that change by updating the plan rather than saving a memory.",
        "- When to use or update tasks instead of memory: When you need to break your work in current conversation into discrete steps or keep track of your progress use tasks instead of saving to memory. Tasks are great for persisting information about the work that needs to be done in the current conversation, but memory should be reserved for information that will be useful in future conversations.",
        "",
        *(extra_guidelines or []),
        "",
    ]

    # The searching-past-context section is disabled by default, so nothing is appended here.
    return lines


async def load_memory_prompt() -> str | None:
    """Load the unified memory prompt for inclusion in the system prompt."""
    auto_enabled = is_auto_memory_enabled()

    # skip_index gate + cowork extra-guidelines default to off / unset.
    skip_index = False
    cowork_extra = os.environ.get("TABVIS_MEMORY_EXTRA_GUIDELINES")
    extra_guidelines = (
        [cowork_extra] if cowork_extra and cowork_extra.strip() else None
    )

    if auto_enabled:
        auto_dir = get_auto_mem_path()
        ensure_memory_dir_exists(auto_dir)
        lines = build_memory_lines("auto memory", auto_dir, extra_guidelines, skip_index)
        entrypoint = await _read_entrypoint(auto_dir)
        if entrypoint["content"].strip():
            entrypoint_path = os.path.join(auto_dir, ENTRYPOINT_NAME)
            lines.extend(
                [
                    f"Contents of {entrypoint_path} (user's auto-memory, persists across conversations):",
                    "",
                    entrypoint["content"].rstrip(),
                ]
            )
            if entrypoint["truncated"]:
                lines.extend(
                    [
                        "",
                        f"[{ENTRYPOINT_NAME} truncated to {MAX_ENTRYPOINT_LINES} lines / "
                        f"{MAX_ENTRYPOINT_BYTES} UTF-8 bytes]",
                    ]
                )
        return "\n".join(lines)

    return None
