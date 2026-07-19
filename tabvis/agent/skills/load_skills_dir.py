"""Skill-directory loader.

Scans the two un-gated skill scopes for skill files and builds a :class:`PromptCommand` per skill:

* ``<cwd>/.tabvis/skills/`` (``source="projectSettings"``)
* ``~/.tabvis/skills/``     (``source="userSettings"``)

A *skill file* is either ``<name>/SKILL.md`` (directory form — the canonical ``/skills/`` shape;
the skill name is the parent directory name) or ``<name>.md`` (single-file form). Each file has YAML
frontmatter (``name`` / ``description`` / ``allowed-tools`` / ``model`` / ``argument-hint`` /
``arguments``) followed by a body that is the skill prompt. The built command's
:meth:`get_prompt_for_command` returns ``[{"type": "text", "text": <body with $ARGUMENTS
substituted>}]``.

Frontmatter splitting mirrors :mod:`tabvis.agent.tools.agent_defs` (a leading ``---`` fence parsed as YAML
via ``pyyaml``); argument substitution supports full ``$ARGUMENTS``, indexed ``$ARGUMENTS[n]`` /
``$n``, and named ``$foo`` placeholders.

Not supported in this build: MCP skills, the ``policySettings``/managed
scope, the upward ``.tabvis`` project walk, ``--add-dir`` / ``--bare`` handling, namespacing,
symlink/inode dedup, hooks-on-skill, paths-gating (conditional skills), ``effort``,
``context: fork``, ``${TABVIS_SKILL_DIR}`` / ``${TABVIS_SESSION_ID}`` expansion, and inline ``!`...``
shell execution. The legacy ``/commands/`` loader is folded in only as the single-file ``<name>.md``
support here (no namespacing).
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

import yaml

from tabvis.types.command import ContentBlockParam, PromptCommand
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

if TYPE_CHECKING:
    from tabvis.tool import ToolUseContext

__all__ = ["load_skills_dir"]

_FRONTMATTER_DELIM = "---"
_SKILL_FILE_NAME = "SKILL.md"


# --------------------------------------------------------------------------------------------
# Frontmatter split (mirrors tabvis.agent.tools.agent_defs._split_frontmatter)
# --------------------------------------------------------------------------------------------


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split ``--- yaml --- body`` markdown into ``(frontmatter, body)``.

    The file must start with a ``---`` line; the block up to the next ``---`` line is parsed as
    YAML, the remainder is the body. Files without a leading frontmatter fence yield ``({}, raw)``.
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return {}, raw
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            closing = i
            break
    if closing is None:
        return {}, raw
    fm_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, body
    if not isinstance(parsed, dict):
        return {}, body
    return parsed, body


# --------------------------------------------------------------------------------------------
# Frontmatter field parsing
# --------------------------------------------------------------------------------------------


def _parse_allowed_tools(value: Any) -> list[str]:
    """Parse the ``allowed-tools`` frontmatter field.

    Missing/empty -> ``[]``. A list/string containing ``*`` -> ``["*"]``. Otherwise the concrete
    tool-name list (comma- or whitespace-separated when given as a single string).
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [t.strip() for t in value.replace(",", " ").split() if t.strip()]
    elif isinstance(value, list):
        items = [str(t).strip() for t in value if str(t).strip()]
    else:
        return []
    if "*" in items:
        return ["*"]
    return items


def _parse_argument_names(value: Any) -> list[str]:
    """Parse the ``arguments`` frontmatter field: space-separated string or list; drop empty/numeric names."""
    if not value:
        return []
    if isinstance(value, str):
        candidates = value.split()
    elif isinstance(value, list):
        candidates = [str(v) for v in value]
    else:
        return []
    return [name for name in candidates if name.strip() and not re.fullmatch(r"\d+", name.strip())]


def _extract_description_from_markdown(content: str, default: str) -> str:
    """Fallback description: first non-empty line, header prefix stripped."""
    for line in content.split("\n"):
        trimmed = line.strip()
        if trimmed:
            header = re.match(r"^#+\s+(.+)$", trimmed)
            text = header.group(1) if header else trimmed
            return text[:97] + "..." if len(text) > 100 else text
    return default


# --------------------------------------------------------------------------------------------
# Argument substitution (no shell-quote-aware parsing)
# --------------------------------------------------------------------------------------------


def _parse_arguments(args: str) -> list[str]:
    """Split an arguments string into tokens (bounded: simple whitespace split).

    Shell-quote-aware parsing is not supported; this uses a plain whitespace split.
    """
    if not args or not args.strip():
        return []
    return [tok for tok in args.split() if tok]


def _substitute_arguments(
    content: str,
    args: str | None,
    *,
    append_if_no_placeholder: bool = True,
    argument_names: list[str] | None = None,
) -> str:
    """Substitute $ARGUMENTS / $ARGUMENTS[n] / $n / named $foo placeholders."""
    if args is None:
        return content

    argument_names = argument_names or []
    parsed = _parse_arguments(args)
    original = content

    # Named arguments ($foo, $bar) -> positional values.
    for i, name in enumerate(argument_names):
        if not name:
            continue
        value = parsed[i] if i < len(parsed) else ""
        content = re.sub(rf"\${re.escape(name)}(?![\[\w])", value.replace("\\", "\\\\"), content)

    # Indexed arguments $ARGUMENTS[0], $ARGUMENTS[1], ...
    def _indexed(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return parsed[idx] if idx < len(parsed) else ""

    content = re.sub(r"\$ARGUMENTS\[(\d+)\]", _indexed, content)

    # Shorthand $0, $1, ...
    def _shorthand(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return parsed[idx] if idx < len(parsed) else ""

    content = re.sub(r"\$(\d+)(?!\w)", _shorthand, content)

    # Full $ARGUMENTS.
    content = content.replace("$ARGUMENTS", args)

    if content == original and append_if_no_placeholder and args:
        content = content + f"\n\nARGUMENTS: {args}"
    return content


# --------------------------------------------------------------------------------------------
# Skill command builder
# --------------------------------------------------------------------------------------------


def _create_skill_command(
    *,
    skill_name: str,
    display_name: str | None,
    description: str,
    has_user_specified_description: bool,
    markdown_content: str,
    allowed_tools: list[str],
    argument_hint: str | None,
    argument_names: list[str],
    when_to_use: str | None,
    version: str | None,
    model: str | None,
    user_invocable: bool,
    source: str,
    base_dir: str | None,
    loaded_from: str,
) -> PromptCommand:
    """Build a :class:`PromptCommand` whose prompt is the (arg-substituted) skill body."""

    async def get_prompt_for_command(
        args: str, context: ToolUseContext
    ) -> list[ContentBlockParam]:
        final_content = _substitute_arguments(
            markdown_content,
            args,
            append_if_no_placeholder=True,
            argument_names=argument_names,
        )
        return [{"type": "text", "text": final_content}]

    # Returns the display name if set, otherwise the skill name.
    def user_facing_name() -> str:
        return display_name or skill_name

    return PromptCommand(
        name=skill_name,
        description=description,
        get_prompt_for_command=get_prompt_for_command,
        source=source,
        progress_message="running",
        content_length=len(markdown_content),
        allowed_tools=allowed_tools,
        argument_hint=argument_hint,
        arg_names=argument_names or None,
        when_to_use=when_to_use,
        version=version,
        model=model,
        disable_model_invocation=False,
        user_invocable=user_invocable,
        has_user_specified_description=has_user_specified_description,
        is_hidden=not user_invocable,
        loaded_from=loaded_from,
        skill_root=base_dir,
        user_facing_name=user_facing_name if display_name else None,
    )


def _parse_skill_model(value: Any) -> str | None:
    """Bounded ``model`` parse: ``inherit`` -> None; else the trimmed string."""
    if not isinstance(value, str) or not value.strip():
        return None
    trimmed = value.strip()
    return None if trimmed.lower() == "inherit" else trimmed


def _build_skill_from_content(
    *,
    skill_name: str,
    content: str,
    source: str,
    base_dir: str | None,
    loaded_from: str,
    description_fallback: str,
) -> PromptCommand:
    """Parse frontmatter + body and build a :class:`PromptCommand`."""
    frontmatter, body = _split_frontmatter(content)

    raw_description = frontmatter.get("description")
    has_user_description = isinstance(raw_description, str) and raw_description.strip() != ""
    description = (
        raw_description.strip()
        if has_user_description
        else _extract_description_from_markdown(body, description_fallback)
    )

    raw_name = frontmatter.get("name")
    display_name = str(raw_name) if raw_name is not None else None

    user_invocable_raw = frontmatter.get("user-invocable")
    user_invocable = True if user_invocable_raw is None else bool(user_invocable_raw)

    when_to_use = frontmatter.get("when_to_use")
    version = frontmatter.get("version")

    return _create_skill_command(
        skill_name=skill_name,
        display_name=display_name,
        description=description,
        has_user_specified_description=has_user_description,
        markdown_content=body,
        allowed_tools=_parse_allowed_tools(frontmatter.get("allowed-tools")),
        argument_hint=(
            str(frontmatter["argument-hint"])
            if frontmatter.get("argument-hint") is not None
            else None
        ),
        argument_names=_parse_argument_names(frontmatter.get("arguments")),
        when_to_use=when_to_use if isinstance(when_to_use, str) else None,
        version=version if isinstance(version, str) else None,
        model=_parse_skill_model(frontmatter.get("model")),
        user_invocable=user_invocable,
        source=source,
        base_dir=base_dir,
        loaded_from=loaded_from,
    )


# --------------------------------------------------------------------------------------------
# Directory scan
# --------------------------------------------------------------------------------------------


def _scan_skills_dir(directory: str, source: str) -> list[PromptCommand]:
    """Scan a single ``.tabvis/skills`` dir for ``<name>/SKILL.md`` or ``<name>.md`` skills."""
    skills: list[PromptCommand] = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return skills

    for entry in entries:
        entry_path = os.path.join(directory, entry)

        # Directory form: <name>/SKILL.md (skill name = directory name).
        if os.path.isdir(entry_path):
            skill_file = os.path.join(entry_path, _SKILL_FILE_NAME)
            try:
                with open(skill_file, encoding="utf-8") as fh:
                    content = fh.read()
            except FileNotFoundError:
                continue
            except OSError as exc:
                log_for_debugging(f"[skills] failed to read {skill_file}: {exc}")
                continue
            skill = _build_skill_from_content(
                skill_name=entry,
                content=content,
                source=source,
                base_dir=entry_path,
                loaded_from="skills",
                description_fallback="Skill",
            )
            skills.append(skill)
            continue

        # Single-file form: <name>.md (skill name = filename without extension).
        # SKILL.md at the top level has no parent skill dir, so skip it here.
        if entry.endswith(".md") and entry != _SKILL_FILE_NAME:
            try:
                with open(entry_path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError as exc:
                log_for_debugging(f"[skills] failed to read {entry_path}: {exc}")
                continue
            skill = _build_skill_from_content(
                skill_name=entry[: -len(".md")],
                content=content,
                source=source,
                base_dir=None,
                loaded_from="commands_DEPRECATED",
                description_fallback="Custom command",
            )
            skills.append(skill)

    return skills


def load_skills_dir(cwd: str | None = None) -> list[PromptCommand]:
    """Load skills from ``<cwd>/.tabvis/skills/`` and ``~/.tabvis/skills/``.

    Scans only the project (``cwd``-level, not the full upward walk) and user-config skill
    scopes. Project skills come first so they take precedence in a downstream override/dedup
    map. Returns ``[]`` when neither scope yields a skill.
    """
    cwd = cwd or get_cwd()
    project_dir = os.path.join(cwd, ".tabvis", "skills")
    user_dir = os.path.join(get_tabvis_config_home_dir(), "skills")

    skills: list[PromptCommand] = []
    # Project skills first (highest precedence), then user skills.
    skills.extend(_scan_skills_dir(project_dir, "projectSettings"))
    skills.extend(_scan_skills_dir(user_dir, "userSettings"))
    return skills
