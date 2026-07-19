"""Tool error formatting + classification

The TS module exports ``formatError``/``getErrorParts`` (shell-aware error rendering) and
``formatZodValidationError`` (turns a Zod validation error into a human-readable, LLM-friendly
message). Pydantic replaces Zod in the Python implementation, so ``format_zod_validation_error`` becomes
:func:`format_pydantic_validation_error`, which consumes a :class:`pydantic.ValidationError`.

Two more functions that the call site in ``services/tools/toolExecution.ts`` pairs with the
validation formatter live here for cohesion (they are co-located at dispatch time in the TS
``checkPermissionsAndCallTool``):

* :func:`classify_tool_error`
  caught error to a telemetry-safe label.
* :func:`build_schema_not_sent_hint`
  (``toolExecution.ts:590``); appends a "re-load the tool" hint when a deferred tool's schema was
  never sent to the API.

Casing: Python identifiers are snake_case; the wire-shaped message dicts that flow through here
keep their Anthropic/transcript keys (``type``, ``tool_name``, ``content``, ...).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tabvis.utils.errors import get_errno_code

if TYPE_CHECKING:
    from tabvis.tool import Tool
    from tabvis.types.message import Message

# --- locally-stubbed constants/classes ----------------------------------------------------------
# The full taxonomy in src/utils/errors.ts (AbortError, ShellError, TelemetrySafeError) and the
# message constants in src/utils/messages.ts are implemented in later waves. We define the minimal
# surface used here so the module stays importable and behaviorally faithful.

# src/utils/messages.ts:213
INTERRUPT_MESSAGE_FOR_TOOL_USE = "[Request interrupted by user for tool use]"

# src/tools/ToolSearchTool/constants.ts:1
TOOL_SEARCH_TOOL_NAME = "ToolSearch"

# Cap mirroring formatError's truncation window (toolErrors.ts:15-21).
_FORMAT_ERROR_MAX_CHARS = 10000
_FORMAT_ERROR_HALF = 5000


class AbortError(Exception):
    """Local alias for the abort-shaped error.

    tabvis.utils.abort also defines an ``AbortError``; both are treated as abort-shaped by
    :func:`format_error`.
    """


class ShellError(Exception):
    """Shell-command error used by :func:`get_error_parts`.

    Only ``stdout``/``stderr``/``code``/``interrupted`` are read here.
    """

    def __init__(self, stdout: str, stderr: str, code: int, interrupted: bool) -> None:
        super().__init__("Shell command failed")
        self.stdout = stdout
        self.stderr = stderr
        self.code = code
        self.interrupted = interrupted


class TelemetrySafeError(Exception):
    """Error whose message is vetted as safe to log to telemetry.

    Mirrors ``TelemetrySafeError_I_VERIFIED_THIS_IS_NOT_CODE_OR_FILEPATHS``.
    """

    def __init__(self, message: str, telemetry_message: str | None = None) -> None:
        super().__init__(message)
        self.telemetry_message = telemetry_message if telemetry_message is not None else message


def _is_abort_error(error: Any) -> bool:
    """True for any abort-shaped error (our :class:`AbortError`, tabvis.utils.abort's, or a
    DOMException-style error whose name is ``'AbortError'``)."""
    if isinstance(error, AbortError):
        return True
    # tabvis.utils.abort.AbortError, imported lazily to avoid a hard dependency cycle.
    try:
        from tabvis.utils.abort import AbortError as _AbortShim

        if isinstance(error, _AbortShim):
            return True
    except Exception:  # noqa: BLE001 - defensive; the shim should always import
        pass
    return getattr(error, "name", None) == "AbortError"


# --- formatError / getErrorParts (toolErrors.ts:5-41) --------------------------------------------
def format_error(error: Any) -> str:
    """Render an arbitrary caught error into a single string,
    truncating very large outputs around a 10k-char window."""
    if _is_abort_error(error):
        return str(error) or INTERRUPT_MESSAGE_FOR_TOOL_USE
    if not isinstance(error, BaseException):
        return str(error)

    parts = get_error_parts(error)
    full_message = "\n".join(p for p in parts if p).strip() or "Command failed with no output"
    if len(full_message) <= _FORMAT_ERROR_MAX_CHARS:
        return full_message
    start = full_message[:_FORMAT_ERROR_HALF]
    end = full_message[-_FORMAT_ERROR_HALF:]
    truncated = len(full_message) - _FORMAT_ERROR_MAX_CHARS
    return f"{start}\n\n... [{truncated} characters truncated] ...\n\n{end}"


def get_error_parts(error: BaseException) -> list[str]:
    """Decompose an error into its message/stderr/stdout parts."""
    if isinstance(error, ShellError):
        return [
            f"Exit code {error.code}",
            INTERRUPT_MESSAGE_FOR_TOOL_USE if error.interrupted else "",
            error.stderr,
            error.stdout,
        ]
    parts = [str(error)]
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, str):
        parts.append(stderr)
    stdout = getattr(error, "stdout", None)
    if isinstance(stdout, str):
        parts.append(stdout)
    return parts


# --- classifyToolError (toolExecution.ts:164) ----------------------------------------------------
def classify_tool_error(error: Any) -> str:
    """Map a caught error to a telemetry-safe label.

    Order (matches the TS):
      1. :class:`TelemetrySafeError` → its vetted ``telemetry_message`` (capped at 200 chars).
      2. errno-bearing errors (ENOENT, EACCES, ...) → ``Error:<CODE>``.
      3. Other exceptions with a stable, non-mangled class name → that name (capped at 60).
      4. Fallback for an ``Exception`` → ``"Error"``.
      5. Non-exception values → ``"UnknownError"``.
    """
    if isinstance(error, TelemetrySafeError):
        return error.telemetry_message[:200]
    if isinstance(error, BaseException):
        errno_code = get_errno_code(error)
        if isinstance(errno_code, str):
            return f"Error:{errno_code}"
        # In Python, class names are not minified, so this is straightforward: prefer the concrete
        # class name when it's something more specific than the bare ``Exception``/``Error`` base.
        name = type(error).__name__
        if name and name not in ("Error", "Exception") and len(name) > 3:
            return name[:60]
        return "Error"
    return "UnknownError"


# --- formatPydanticValidationError (replaces formatZodValidationError, toolErrors.ts:66) ----------
def _format_validation_path(path: tuple[Any, ...]) -> str:
    """E.g. ``('todos', 0, 'active_form')`` => ``todos[0].active_form``."""
    if not path:
        return ""
    acc = ""
    for index, segment in enumerate(path):
        if isinstance(segment, int):
            acc = f"{acc}[{segment}]"
        elif index == 0:
            acc = str(segment)
        else:
            acc = f"{acc}.{segment}"
    return acc


# Map pydantic error-``type`` prefixes to a human-friendly "expected" type label, mirroring the
# `expected` field Zod surfaces. The prefix before ``_type``/``_parsing`` names the target type.
_EXPECTED_TYPE_LABELS: dict[str, str] = {
    "string": "string",
    "int": "number",
    "float": "number",
    "decimal": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
    "set": "array",
    "tuple": "array",
    "frozenset": "array",
    "bytes": "bytes",
    "none": "null",
}

# pydantic error ``type`` strings that denote a primitive type/parse mismatch (the analogue of
# Zod's ``invalid_type`` *without* "received undefined").
_TYPE_MISMATCH_SUFFIXES = ("_type", "_parsing")


def _python_type_label(value: Any) -> str:
    """Best-effort "received" label, mirroring Zod's ``received <type>``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _expected_label_for(error_type: str) -> str | None:
    if not error_type.endswith(_TYPE_MISMATCH_SUFFIXES):
        return None
    prefix = error_type.rsplit("_", 1)[0]
    return _EXPECTED_TYPE_LABELS.get(prefix, prefix)


def format_pydantic_validation_error(tool_name: str, error: ValidationError) -> str:
    """Replacement for ``formatZodValidationError``: render a :class:`pydantic.ValidationError`
    into a human-readable, LLM-friendly message.

    Groups the underlying issues into the same three buckets the TS produced:
      * missing required parameters (``type == 'missing'``),
      * unexpected parameters (``type == 'extra_forbidden'``),
      * type mismatches (``*_type`` / ``*_parsing``),
    and falls back to pydantic's own rendering when none of those apply.
    """
    issues = error.errors(include_url=False)

    missing_params: list[str] = []
    unexpected_params: list[str] = []
    type_mismatch_params: list[dict[str, str]] = []

    for issue in issues:
        etype = str(issue.get("type", ""))
        loc = tuple(issue.get("loc", ()))
        if etype == "missing":
            missing_params.append(_format_validation_path(loc))
        elif etype == "extra_forbidden":
            # The unexpected key is the final path segment.
            unexpected_params.append(str(loc[-1]) if loc else "")
        else:
            expected = _expected_label_for(etype)
            if expected is None:
                continue
            type_mismatch_params.append(
                {
                    "param": _format_validation_path(loc),
                    "expected": expected,
                    "received": _python_type_label(issue.get("input")),
                }
            )

    # Default to pydantic's own message if we can't build a better one.
    error_content = str(error)
    error_parts: list[str] = []

    error_parts.extend(
        f"The required parameter `{param}` is missing" for param in missing_params
    )
    error_parts.extend(
        f"An unexpected parameter `{param}` was provided" for param in unexpected_params
    )
    error_parts.extend(
        f"The parameter `{p['param']}` type is expected as `{p['expected']}` "
        f"but provided as `{p['received']}`"
        for p in type_mismatch_params
    )

    if error_parts:
        noun = "issues" if len(error_parts) > 1 else "issue"
        error_content = f"{tool_name} failed due to the following {noun}:\n" + "\n".join(error_parts)

    return error_content


# --- buildSchemaNotSentHint (toolExecution.ts:590) -----------------------------------------------
def _is_tool_search_enabled_optimistic() -> bool:
    """Optimistic gate for tool search.

    This build uses a heuristic rather than full first-party provider detection: enabled unless
    tool search mode is the default 'standard'. A truthy ``ENABLE_TOOL_SEARCH`` env var is an
    explicit opt-in.
    """
    mode = os.environ.get("TOOL_SEARCH_MODE", "")
    if mode == "standard":
        return False
    return bool(os.environ.get("ENABLE_TOOL_SEARCH"))


def _is_tool_search_tool_available(tools: Any) -> bool:
    """Return whether tool search tool available."""
    for tool in tools or []:
        name = getattr(tool, "name", None) if not isinstance(tool, dict) else tool.get("name")
        aliases = (
            getattr(tool, "aliases", None) if not isinstance(tool, dict) else tool.get("aliases")
        )
        if name == TOOL_SEARCH_TOOL_NAME or TOOL_SEARCH_TOOL_NAME in (aliases or []):
            return True
    return False


def _is_deferred_tool(tool: Tool) -> bool:
    """Return whether deferred tool."""
    if getattr(tool, "always_load", False) is True:
        return False
    if getattr(tool, "is_mcp", False) is True:
        return True
    if getattr(tool, "name", None) == TOOL_SEARCH_TOOL_NAME:
        return False
    return getattr(tool, "should_defer", False) is True


def _extract_discovered_tool_names(messages: list[Message]) -> set[str]:
    """Extract the discovered tool names.

    Walks the transcript collecting tool names that were expanded via ``tool_reference`` blocks
    inside ToolSearch tool_result content, plus any carried across a ``compact_boundary``.
    """
    discovered: set[str] = set()
    for msg in messages or []:
        msg_type = msg.get("type") if isinstance(msg, dict) else None
        if msg_type == "system" and (
            isinstance(msg, dict) and msg.get("subtype") == "compact_boundary"
        ):
            carried = (msg.get("compactMetadata") or {}).get("preCompactDiscoveredTools")
            if carried:
                discovered.update(carried)
            continue
        if msg_type != "user":
            continue
        message = msg.get("message") if isinstance(msg, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if not isinstance(inner, list):
                continue
            for item in inner:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "tool_reference"
                    and isinstance(item.get("tool_name"), str)
                ):
                    discovered.add(item["tool_name"])
    return discovered


def build_schema_not_sent_hint(
    tool: Tool,
    messages: list[Message],
    tools: Any,
) -> str | None:
    """Returns a hint to re-load a deferred tool whose schema
    was never sent to the API, or ``None`` when the schema was sent / tool search is unavailable."""
    if not _is_tool_search_enabled_optimistic():
        return None
    if not _is_tool_search_tool_available(tools):
        return None
    if not _is_deferred_tool(tool):
        return None
    discovered = _extract_discovered_tool_names(messages)
    if tool.name in discovered:
        return None
    return (
        "\n\nThis tool's schema was not sent to the API — it was not in the discovered-tool set "
        "derived from message history. Without the schema in your prompt, typed parameters "
        "(arrays, numbers, booleans) get emitted as strings and the client-side parser rejects "
        f'them. Load the tool first: call {TOOL_SEARCH_TOOL_NAME} with query "select:{tool.name}", '
        "then retry this call."
    )
