"""Settings-validation tips

Maps a validation-error context (path + issue code + expected/received/enum values) to a
human-readable ``suggestion`` + optional ``docLink``. Used to enrich the settings-validation error
output. The zod ``ZodIssueCode`` type the TS imports is value-only there (for typing) — codes are
plain strings here (``invalid_value`` / ``invalid_type`` / ``too_small`` / ``unrecognized_keys`` /
...), matching what the validator emits.

Casing: Python identifiers snake_case; the returned :class:`ValidationTip` keeps the TS wire field
name ``docLink`` (it round-trips to the same error payload).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ValidationTip:
    """A tip for a validation error."""

    suggestion: str | None = None
    doc_link: str | None = None  # wire name: docLink


@dataclass
class TipContext:
    """Context describing a single validation issue."""

    path: str
    code: str
    expected: str | None = None
    received: Any = None
    enum_values: list[str] | None = None  # wire name: enumValues
    message: str | None = None
    value: Any = None


@dataclass
class _TipMatcher:
    matches: Callable[[TipContext], bool]
    tip: ValidationTip


DOCUMENTATION_BASE = "https://code.tabvis.com/docs/en"


_TIP_MATCHERS: list[_TipMatcher] = [
    _TipMatcher(
        matches=lambda ctx: ctx.path == "permissions.defaultMode"
        and ctx.code == "invalid_value",
        tip=ValidationTip(
            suggestion=(
                'Valid modes: "acceptEdits" (ask before file changes), "plan" (analysis only), '
                '"bypassPermissions" (auto-accept all), or "default" (standard behavior)'
            ),
            doc_link=f"{DOCUMENTATION_BASE}/iam#permission-modes",
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: ctx.path == "apiKeyHelper" and ctx.code == "invalid_type",
        tip=ValidationTip(
            suggestion=(
                "Provide a shell command that outputs your API key to stdout. The script should "
                'output only the API key. Example: "/bin/generate_temp_api_key.sh"'
            ),
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: ctx.path == "cleanupPeriodDays"
        and ctx.code == "too_small"
        and ctx.expected == "0",
        tip=ValidationTip(
            suggestion=(
                "Must be 0 or greater. Set a positive number for days to retain transcripts "
                "(default is 30). Setting 0 disables session persistence entirely: no transcripts "
                "are written and existing transcripts are deleted at startup."
            ),
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: ctx.path.startswith("env.") and ctx.code == "invalid_type",
        tip=ValidationTip(
            suggestion=(
                "Environment variables must be strings. Wrap numbers and booleans in quotes. "
                'Example: "DEBUG": "true", "PORT": "3000"'
            ),
            doc_link=f"{DOCUMENTATION_BASE}/settings#environment-variables",
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: (
            ctx.path in ("permissions.allow", "permissions.deny")
            and ctx.code == "invalid_type"
            and ctx.expected == "array"
        ),
        tip=ValidationTip(
            suggestion=(
                'Permission rules must be in an array. Format: ["Tool(specifier)"]. Examples: '
                '["Bash(npm run build)", "Edit(docs/**)", "Read(~/.zshrc)"]. Use * for wildcards.'
            ),
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: "hooks" in ctx.path and ctx.code == "invalid_type",
        tip=ValidationTip(
            # gh-31187 / CC-282: prior example showed {"matcher": {"tools": ["BashTool"]}}
            # — an object format that never existed in the schema (matcher is z.string(), always
            # has been). Users copied the tip's example and got the same validation error again.
            # See matchesPattern() in hooks.ts: matcher is exact-match, pipe-separated
            # ("Edit|Write"), or regex. Empty/"*" matches all.
            suggestion=(
                "Hooks use a matcher + hooks array. The matcher is a string: a tool name "
                '("Bash"), pipe-separated list ("Edit|Write"), or empty to match all. Example: '
                '{"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", '
                '"command": "echo Done"}]}]}'
            ),
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: ctx.code == "invalid_type" and ctx.expected == "boolean",
        tip=ValidationTip(
            suggestion=(
                'Use true or false without quotes. Example: "includeCoAuthoredBy": true'
            ),
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: ctx.code == "unrecognized_keys",
        tip=ValidationTip(
            suggestion="Check for typos or refer to the documentation for valid fields",
            doc_link=f"{DOCUMENTATION_BASE}/settings",
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: ctx.code == "invalid_value" and ctx.enum_values is not None,
        tip=ValidationTip(
            suggestion=None,
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: (
            ctx.code == "invalid_type"
            and ctx.expected == "object"
            and ctx.received is None
            and ctx.path == ""
        ),
        tip=ValidationTip(
            suggestion=(
                "Check for missing commas, unmatched brackets, or trailing commas. Use a JSON "
                "validator to identify the exact syntax error."
            ),
        ),
    ),
    _TipMatcher(
        matches=lambda ctx: ctx.path == "permissions.additionalDirectories"
        and ctx.code == "invalid_type",
        tip=ValidationTip(
            suggestion=(
                'Must be an array of directory paths. Example: ["~/projects", "/tmp/workspace"]. '
                "You can also use --add-dir flag or /add-dir command"
            ),
            doc_link=f"{DOCUMENTATION_BASE}/iam#working-directories",
        ),
    ),
]


_PATH_DOC_LINKS: dict[str, str] = {
    "permissions": f"{DOCUMENTATION_BASE}/iam#configuring-permissions",
    "env": f"{DOCUMENTATION_BASE}/settings#environment-variables",
    "hooks": f"{DOCUMENTATION_BASE}/hooks",
}


def get_validation_tip(context: TipContext) -> ValidationTip | None:
    """Return the best :class:`ValidationTip` for ``context``.

    Finds the first matching matcher; ``None`` if none match. For an ``invalid_value`` issue with
    enum values and no canned suggestion, synthesizes a "Valid values: ..." suggestion. Fills in a
    ``doc_link`` from the path prefix when the matched tip has none.
    """
    matcher = next((m for m in _TIP_MATCHERS if m.matches(context)), None)

    if matcher is None:
        return None

    # Copy the tip so we don't mutate the shared matcher table.
    tip = ValidationTip(suggestion=matcher.tip.suggestion, doc_link=matcher.tip.doc_link)

    if context.code == "invalid_value" and context.enum_values and not tip.suggestion:
        values = ", ".join(f'"{v}"' for v in context.enum_values)
        tip.suggestion = f"Valid values: {values}"

    # Add a documentation link based on the path prefix.
    if not tip.doc_link and context.path:
        path_prefix = context.path.split(".")[0]
        if path_prefix:
            tip.doc_link = _PATH_DOC_LINKS.get(path_prefix)

    return tip
