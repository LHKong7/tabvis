"""Load lightweight project instructions from ``TABVIS.md`` files.

The loader intentionally supports one convention only: a ``TABVIS.md`` file in
each directory from the repository root to the current working directory. Extra
working directories may contribute their own top-level ``TABVIS.md``. There is no
frontmatter, include syntax, rule matching, attachment expansion, or cache.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TypedDict

from tabvis.bootstrap.state import (
    get_additional_directories_for_tabvis_md,
    get_project_root,
)
from tabvis.utils.cwd import get_cwd
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.git import find_git_root

INSTRUCTION_FILE_NAME = "TABVIS.md"
DISABLE_ENV_VAR = "TABVIS_DISABLE_TABVIS_MDS"
MAX_INSTRUCTION_LINES = 200
MAX_INSTRUCTION_BYTES = 40_000
MAX_TOTAL_INSTRUCTION_BYTES = 80_000


class LoadedInstruction(TypedDict):
    path: str
    content: str
    truncated: bool


def _normalized_path(path: str, *, relative_to: str | None = None) -> str:
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(relative_to or get_cwd(), expanded)
    return os.path.realpath(os.path.abspath(expanded))


def _path_key(path: str) -> str:
    return os.path.normcase(_normalized_path(path))


def _is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def _directories_from_root(root: str, cwd: str) -> list[str]:
    """Return ``root`` through ``cwd`` in broad-to-specific order."""
    if not _is_within(cwd, root):
        return [cwd]

    relative = os.path.relpath(cwd, root)
    directories = [root]
    if relative == ".":
        return directories

    current = root
    for part in Path(relative).parts:
        current = os.path.join(current, part)
        directories.append(current)
    return directories


def discover_instruction_paths(
    additional_directories: list[str] | None = None,
) -> list[str]:
    """Discover candidate ``TABVIS.md`` paths in precedence order.

    The repository hierarchy is broad-to-specific. Explicit extra directories
    follow it, so their instructions have the highest precedence. Duplicate paths
    (including symlink aliases) are returned only once.
    """
    cwd = _normalized_path(get_cwd())
    configured_root = _normalized_path(get_project_root())
    root = _normalized_path(find_git_root(configured_root) or configured_root)

    # If the process moved outside the session project, use the current repository
    # rather than walking from a filesystem ancestor such as the user's home.
    if not _is_within(cwd, root):
        root = _normalized_path(find_git_root(cwd) or cwd)

    directories = _directories_from_root(root, cwd)
    directories.extend(get_additional_directories_for_tabvis_md())
    directories.extend(additional_directories or [])

    paths: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        normalized_dir = _normalized_path(directory, relative_to=cwd)
        instruction_path = os.path.join(normalized_dir, INSTRUCTION_FILE_NAME)
        key = _path_key(instruction_path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(instruction_path)
    return paths


def _truncate_content(content: str, byte_limit: int) -> tuple[str, bool]:
    line_limited = "".join(content.splitlines(keepends=True)[:MAX_INSTRUCTION_LINES])
    encoded = line_limited.encode("utf-8")
    byte_limited = (
        encoded[:byte_limit].decode("utf-8", errors="ignore")
        if len(encoded) > byte_limit
        else line_limited
    )
    return byte_limited, byte_limited != content


async def load_instruction_files(
    additional_directories: list[str] | None = None,
) -> list[LoadedInstruction]:
    """Read discovered files with per-file and aggregate context limits.

    When the aggregate limit is reached, more-specific files are kept before
    broader ones because they have higher precedence.
    """
    candidates: list[LoadedInstruction] = []
    for path in discover_instruction_paths(additional_directories):
        try:
            content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if not content.strip():
            continue
        truncated_content, truncated = _truncate_content(content, MAX_INSTRUCTION_BYTES)
        candidates.append(
            {"path": path, "content": truncated_content, "truncated": truncated}
        )

    remaining = MAX_TOTAL_INSTRUCTION_BYTES
    retained_reversed: list[LoadedInstruction] = []
    for instruction in reversed(candidates):
        if remaining <= 0:
            break
        content, total_truncated = _truncate_content(instruction["content"], remaining)
        if not content.strip():
            continue
        retained_reversed.append(
            {
                "path": instruction["path"],
                "content": content,
                "truncated": instruction["truncated"] or total_truncated,
            }
        )
        remaining -= len(content.encode("utf-8"))

    retained_reversed.reverse()
    return retained_reversed


async def load_project_instructions_prompt(
    additional_directories: list[str] | None = None,
) -> str | None:
    """Render project instructions for insertion into the system prompt."""
    if is_env_truthy(os.environ.get(DISABLE_ENV_VAR)):
        return None

    instructions = await load_instruction_files(additional_directories)
    if not instructions:
        return None

    parts = [
        "# Project instructions",
        "",
        f"The following `{INSTRUCTION_FILE_NAME}` files contain user-provided project "
        "guidance. They are listed from broadest to most specific; when instructions "
        "conflict, later files take precedence.",
    ]
    for instruction in instructions:
        parts.extend(
            [
                "",
                f"## `{instruction['path']}`",
                "",
                instruction["content"].rstrip(),
            ]
        )
        if instruction["truncated"]:
            parts.extend(
                [
                    "",
                    f"[{INSTRUCTION_FILE_NAME} truncated to context limits]",
                ]
            )
    return "\n".join(parts)
