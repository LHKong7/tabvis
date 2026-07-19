"""Bash tool for validated, timeout-bound subprocess execution.

  * Run ``command`` via an asyncio subprocess under a timeout (default + max sourced
    from the bash-timeout constants).
  * Capture merged stdout/stderr,
    truncate to ``BASH_MAX_OUTPUT_LENGTH`` (default 30K chars) with an
    ``EndTruncatingAccumulator``-equivalent marker, interpret the exit code with the
    minimal semantic rules, and return the ``Out`` dict.

``check_permissions`` / ``is_read_only`` use the permission and read-only validation modules via
function-local (lazy) imports that break
the ``bash_tool`` <-> consumers import cycle. ``is_concurrency_safe`` derives from the real
``is_read_only`` result.

Not supported in this build:

  * Background tasks (``run_in_background``), sandboxing, sed-edit simulation, progress
    streaming / auto-backgrounding, large-output persistence to ``tool-results``, and
    image resizing. ``run_in_background`` is accepted in the schema for parity but is
    executed in the foreground here.

Casing: Python identifiers are snake_case; the ``Out`` data dict and the returned
``tool_result`` block keep their Anthropic/transcript wire keys (``tool_use_id``,
``is_error``, plus the ``Out`` field names which round-trip to the SDK output schema).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import (
    Tool,
    ToolResult,
    ToolUseContext,
    ValidationResult,
)
from tabvis.utils.cwd import get_cwd
from tabvis.utils.env_utils import is_env_truthy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASH_TOOL_NAME = "Bash"  # src/tools/BashTool/toolName.ts

_DEFAULT_TIMEOUT_MS = 120_000  # 2 minutes
_MAX_TIMEOUT_MS = 600_000  # 10 minutes

BASH_MAX_OUTPUT_UPPER_LIMIT = 150_000
BASH_MAX_OUTPUT_DEFAULT = 30_000

EOL = "\n"
TOOL_SUMMARY_MAX_LENGTH = 50  # src/constants/toolLimits.ts


def _parse_positive_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value, 10)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def get_default_timeout_ms(env: dict[str, str] | None = None) -> int:
    """``BASH_DEFAULT_TIMEOUT_MS`` or 2 minutes."""
    env = os.environ if env is None else env
    parsed = _parse_positive_int(env.get("BASH_DEFAULT_TIMEOUT_MS"))
    return parsed if parsed is not None else _DEFAULT_TIMEOUT_MS


def get_max_timeout_ms(env: dict[str, str] | None = None) -> int:
    """``BASH_MAX_TIMEOUT_MS`` clamped to >= default."""
    env = os.environ if env is None else env
    parsed = _parse_positive_int(env.get("BASH_MAX_TIMEOUT_MS"))
    if parsed is not None:
        return max(parsed, get_default_timeout_ms(env))
    return max(_MAX_TIMEOUT_MS, get_default_timeout_ms(env))


def get_max_output_length(env: dict[str, str] | None = None) -> int:
    """``BASH_MAX_OUTPUT_LENGTH`` bounded [default, upper]."""
    env = os.environ if env is None else env
    raw = _parse_positive_int(env.get("BASH_MAX_OUTPUT_LENGTH"))
    if raw is None:
        return BASH_MAX_OUTPUT_DEFAULT
    # validateBoundedIntEnvVar clamps to the inclusive [BASH_MAX_OUTPUT_DEFAULT,
    # BASH_MAX_OUTPUT_UPPER_LIMIT] window.
    return max(BASH_MAX_OUTPUT_DEFAULT, min(raw, BASH_MAX_OUTPUT_UPPER_LIMIT))


# Background tasks are disabled at module load when TABVIS_DISABLE_BACKGROUND_TASKS is set.
# In this skeleton build we always run in the foreground, but keep the flag to mirror the
# schema-shaping decision (omit run_in_background from the model-facing schema when set).
_IS_BACKGROUND_TASKS_DISABLED = is_env_truthy(os.environ.get("TABVIS_DISABLE_BACKGROUND_TASKS"))


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------


class BashToolInput(BaseModel):
    """Validated arguments for the Bash tool (Zod ``strictObject`` → pydantic forbid-extra).

    ``_simulatedSedEdit`` is intentionally omitted (internal-only, never model-facing).
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(description="The command to execute")
    timeout: int | None = Field(
        default=None,
        description=f"Optional timeout in milliseconds (max {get_max_timeout_ms()})",
    )
    description: str | None = Field(
        default=None,
        description=(
            "Clear, concise description of what this command does in active voice. "
            'Never use words like "complex" or "risk" in the description - just describe '
            "what it does.\n\n"
            "For simple commands (git, npm, standard CLI tools), keep it brief (5-10 words):\n"
            '- ls → "List files in current directory"\n'
            '- git status → "Show working tree status"\n'
            '- npm install → "Install package dependencies"\n\n'
            "For commands that are harder to parse at a glance (piped commands, obscure flags, "
            "etc.), add enough context to clarify what it does:\n"
            '- find . -name "*.tmp" -exec rm {} \\; → "Find and delete all .tmp files recursively"\n'
            '- git reset --hard origin/main → "Discard all local changes and match remote main"\n'
            "- curl -s url | jq '.data[]' → \"Fetch JSON from URL and extract data array elements\""
        ),
    )
    run_in_background: bool | None = Field(
        default=None,
        description=(
            "Set to true to run this command in the background. Use Read to read the output later."
        ),
    )
    dangerouslyDisableSandbox: bool | None = Field(  # noqa: N815 - wire key (model-facing schema)
        default=None,
        description=(
            "Set this to true to dangerously override sandbox mode and run commands "
            "without sandboxing."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers kept behaviorally equivalent from BashTool.ts / utils.ts / commandSemantics.ts
# ---------------------------------------------------------------------------

_IMAGE_DATA_URI_RE = re.compile(r"^data:image/[a-z0-9.+_-]+;base64,", re.IGNORECASE)

# Commands that typically produce no stdout on success (subset used by isSilentBashCommand).
_BASH_SILENT_COMMANDS = frozenset(
    {
        "mv", "cp", "rm", "mkdir", "rmdir", "chmod", "chown", "chgrp",
        "touch", "ln", "cd", "export", "unset", "wait",
    }
)

# Per-command exit-code semantics
# Each entry maps base command -> (error_threshold, {exit_code: message}).
_GREP_LIKE = (2, {1: "No matches found"})
_COMMAND_SEMANTICS: dict[str, tuple[int, dict[int, str]]] = {
    "grep": _GREP_LIKE,
    "rg": _GREP_LIKE,
    "find": (2, {1: "Some directories were inaccessible"}),
    "diff": (2, {1: "Files differ"}),
    "test": (2, {1: "Condition is false"}),
    "[": (2, {1: "Condition is false"}),
}


def is_image_output(content: str) -> bool:
    """True when ``content`` is an image data URI."""
    return bool(_IMAGE_DATA_URI_RE.match(content))


def strip_empty_lines(content: str) -> str:
    """Drop leading/trailing all-whitespace lines."""
    lines = content.split("\n")
    start_index = 0
    while start_index < len(lines) and lines[start_index].strip() == "":
        start_index += 1
    end_index = len(lines) - 1
    while end_index >= 0 and lines[end_index].strip() == "":
        end_index -= 1
    if start_index > end_index:
        return ""
    return "\n".join(lines[start_index : end_index + 1])


def _heuristic_base_command(command: str) -> str:
    """Approximate ``heuristicallyExtractBaseCommand`` for exit-code interpretation.

    The TS splits on shell operators and takes the *last* subcommand (it determines the
    exit code), then its first word. We use a lightweight split on pipes/&&/||/; — good
    enough for the semantic rules and never used for security decisions.
    """
    segments = re.split(r"\s*(?:\|\||&&|;|\|)\s*", command.strip())
    last = segments[-1] if segments and segments[-1] else command
    parts = last.strip().split()
    return parts[0] if parts else ""


def interpret_command_result(
    command: str, exit_code: int, stdout: str, stderr: str
) -> dict[str, Any]:
    """Semantic interpretation of a non-zero exit code.

    Returns ``{"is_error": bool, "message": str | None}``.
    """
    base = _heuristic_base_command(command)
    semantic = _COMMAND_SEMANTICS.get(base)
    if semantic is not None:
        threshold, messages = semantic
        return {"is_error": exit_code >= threshold, "message": messages.get(exit_code)}
    # DEFAULT_SEMANTIC: only 0 is success.
    is_error = exit_code != 0
    return {
        "is_error": is_error,
        "message": f"Command failed with exit code {exit_code}" if is_error else None,
    }


def is_silent_bash_command(command: str) -> bool:
    """Operator-aware split).

    True when every non-fallback subcommand is in the silent set (commands that emit no
    stdout on success), so the UI can show "Done" instead of "(No output)".
    """
    parts = re.split(r"\s*(?:\|\||&&|;|\|)\s*", command.strip())
    has_non_fallback = False
    for part in parts:
        base = part.strip().split()[0] if part.strip().split() else ""
        if not base:
            continue
        has_non_fallback = True
        if base not in _BASH_SILENT_COMMANDS:
            return False
    return has_non_fallback


def truncate_output(full_output: str, max_size: int) -> str:
    """Keep the head, mark the tail.

    The TS appends ``trimEnd(stdout) + EOL`` to the accumulator, so do the same here, then
    cap at ``max_size`` chars and append the ``[output truncated - NKB removed]`` marker.
    """
    body = full_output.rstrip() + EOL
    total = len(body)
    if total <= max_size:
        return body
    truncated_bytes = total - max_size
    truncated_kb = round(truncated_bytes / 1024)
    return body[:max_size] + f"\n... [output truncated - {truncated_kb}KB removed]"


def _truncate_summary(text: str, max_width: int) -> str:
    """Subset of ``truncate`` (src/utils/truncate.ts) for tool-use summaries."""
    if len(text) <= max_width:
        return text
    return text[:max_width] + "…"


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class BashTool(Tool):
    """``Bash`` — execute a shell command and return merged stdout/stderr.

    Required overrides per :class:`tabvis.tool.Tool`: name/input_schema/max_result_size_chars,
    ``call``/``description``/``prompt``/``map_tool_result_to_tool_result_block_param``.
    """

    name = BASH_TOOL_NAME
    search_hint = "execute shell commands"
    # 30K chars — tool result persistence threshold (maxResultSizeChars in TS).
    max_result_size_chars = 30_000
    strict = True
    input_schema = BashToolInput

    # --- discovery / safety flags ---------------------------------------
    def is_read_only(self, input: Any) -> bool:
        # Derive compoundCommandHasCd, run the read-only classifier, and treat an 'allow'
        # decision as read-only.
        # Lazy import breaks the bash_tool <-> read_only_validation/bash_permissions cycle.
        from tabvis.agent.tools.bash_permissions import command_has_any_cd
        from tabvis.agent.tools.read_only_validation import check_read_only_constraints

        compound_command_has_cd = command_has_any_cd(_get(input, "command") or "")
        result = check_read_only_constraints(input, compound_command_has_cd)
        return result.get("behavior") == "allow"

    def is_concurrency_safe(self, input: Any) -> bool:
        # TS: isReadOnly?.(input) ?? false. Now reflects the real read-only decision.
        return self.is_read_only(input)

    async def check_permissions(self, input: Any, context: ToolUseContext):
        # Compose the bash resolver with the policy engine, which layers shell.execute /
        # network.request policy and audits every command. The
        # adapter takes the most restrictive of (resolver, engine), so the resolver is never loosened.
        # Lazy import breaks the bash_tool <-> bash_permissions cycle.
        from tabvis.policy.bash_adapter import evaluate

        return await evaluate(input, context)

    # --- display --------------------------------------------------------
    def user_facing_name(self, input: Any | None = None) -> str:
        return "Bash"

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        command = _get(input, "command")
        if not command:
            return None
        description = _get(input, "description")
        if description:
            return description
        return _truncate_summary(command, TOOL_SUMMARY_MAX_LENGTH)

    def get_activity_description(self, input: Any | None) -> str | None:
        command = _get(input, "command")
        if not command:
            return "Running command"
        desc = _get(input, "description") or _truncate_summary(command, TOOL_SUMMARY_MAX_LENGTH)
        return f"Running {desc}"

    def extract_search_text(self, out: Any) -> str | None:
        stdout = _get(out, "stdout") or ""
        stderr = _get(out, "stderr") or ""
        return f"{stdout}\n{stderr}" if stderr else stdout

    def is_result_truncated(self, output: Any) -> bool:
        # accumulator's end-truncation marker instead.
        text = (_get(output, "stdout") or "") + (_get(output, "stderr") or "")
        return "[output truncated" in text

    # --- required behavior ----------------------------------------------
    async def description(self, input: Any, options: dict[str, Any]) -> str:
        description = _get(input, "description")
        return description or "Run shell command"

    async def prompt(self, options: dict[str, Any]) -> str:
        return _get_simple_prompt()

    async def validate_input(self, input: Any, context: ToolUseContext) -> ValidationResult:
        # The blocked-sleep guard in TS is behind `if (false && ...)` (disabled). Mirror that.
        return ValidationResult(result=True)

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        command: str = args.command
        timeout_ms = args.timeout or get_default_timeout_ms()
        run_in_background = bool(getattr(args, "run_in_background", None))

        if run_in_background and not _IS_BACKGROUND_TASKS_DISABLED:
            pass

        merged, exit_code, interrupted = await _run_shell_command(
            command, timeout_ms, context
        )

        interpretation = interpret_command_result(command, exit_code, merged, "")

        max_output = get_max_output_length()
        accumulated = truncate_output(merged, max_output)
        if interpretation["is_error"] and not interrupted and exit_code != 0:
            accumulated = accumulated + f"Exit code {exit_code}"

        stdout = accumulated

        stripped_stdout = strip_empty_lines(stdout)
        is_image = is_image_output(stripped_stdout)

        data: dict[str, Any] = {
            "stdout": stripped_stdout,
            "stderr": "",
            "interrupted": interrupted,
            "isImage": is_image,
            "returnCodeInterpretation": interpretation["message"],
            "noOutputExpected": is_silent_bash_command(command),
            "dangerouslyDisableSandbox": getattr(args, "dangerouslyDisableSandbox", None),
        }
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        interrupted = bool(_get(content, "interrupted"))
        stdout = _get(content, "stdout") or ""
        stderr = _get(content, "stderr") or ""

        processed_stdout = stdout
        if stdout:
            # Replace any leading newlines or lines with only whitespace, then trim end.
            processed_stdout = re.sub(r"^(\s*\n)+", "", stdout).rstrip()

        error_message = stderr.strip()
        if interrupted:
            if stderr:
                error_message += EOL
            error_message += "<error>Command was aborted before completion</error>"

        joined = "\n".join(part for part in (processed_stdout, error_message) if part)
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": joined,
            "is_error": interrupted,
        }


# ---------------------------------------------------------------------------
# Core subprocess execution — replaces the heavy Shell.ts / ShellCommand.ts machinery.
# ---------------------------------------------------------------------------


async def _run_shell_command(
    command: str, timeout_ms: int, context: ToolUseContext
) -> tuple[str, int, bool]:
    """Run ``command`` via ``bash -c`` with a timeout; return (merged_output, code, interrupted).

    stderr is merged into stdout (the TS tree uses a single merged fd for bash commands).
    On timeout/abort the process is killed and ``interrupted`` is True.
    """
    bash = shutil.which("bash") or "/bin/bash"
    cwd = get_cwd()

    try:
        proc = await asyncio.create_subprocess_exec(
            bash,
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            cwd=cwd,
            start_new_session=True,  # own process group so a timeout can reap the whole tree
        )
    except OSError as err:
        # preSpawnError equivalent (e.g. deleted cwd / missing shell).
        return (f"{err}", 1, False)

    timeout_s = max(timeout_ms, 0) / 1000.0
    interrupted = False
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        interrupted = True
        # Kill the ENTIRE process group, not just the `bash -c` child: a long `forge test` spawns
        # solc/anvil/test grandchildren, and SIGKILLing only the bash PID leaves them alive holding
        # the stdout pipe open -> the post-kill drain below would block forever on EOF and freeze the
        # whole single-threaded agent loop. start_new_session=True above made proc the group leader.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        # Drain whatever was buffered so the model still sees partial output — but BOUND it so a
        # surviving grandchild that kept the pipe open can never hang the loop indefinitely.
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception:  # noqa: BLE001 - best-effort, time-bounded drain
            stdout_bytes = b""

    code = proc.returncode if proc.returncode is not None else (143 if interrupted else 0)
    merged = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    return (merged, code, interrupted)


# ---------------------------------------------------------------------------
# Prompt
# Sandbox + commit/PR sections (gated on config + USER_TYPE) are omitted as out of scope;
# they default to empty strings in a plain headless run.
# ---------------------------------------------------------------------------

# Tool names referenced in the steering bullets. These mirror the TS *_TOOL_NAME constants.
_GLOB_TOOL_NAME = "Glob"
_GREP_TOOL_NAME = "Grep"
_FILE_READ_TOOL_NAME = "Read"
_FILE_EDIT_TOOL_NAME = "Edit"
_FILE_WRITE_TOOL_NAME = "Write"


def _prepend_bullets(items: list[Any]) -> list[str]:
    """Top-level items get ``- ``, nested lists get ``  - ``."""
    lines: list[str] = []
    for item in items:
        if isinstance(item, list):
            for sub in item:
                lines.append(f"  - {sub}")
        else:
            lines.append(f"- {item}")
    return lines


def _get_background_usage_note() -> str | None:
    if _IS_BACKGROUND_TASKS_DISABLED:
        return None
    return (
        "You can use the `run_in_background` parameter to run the command in the background. "
        "Only use this if you don't need the result immediately and are OK being notified when "
        "the command completes later. You do not need to check the output right away - you'll be "
        "notified when it finishes. You do not need to use '&' at the end of the command when "
        "using this parameter."
    )


def _get_simple_prompt() -> str:
    """Return the simple prompt."""
    # so the non-embedded steering bullets are used.
    tool_preference_items = [
        f"File search: Use {_GLOB_TOOL_NAME} (NOT find or ls)",
        f"Content search: Use {_GREP_TOOL_NAME} (NOT grep or rg)",
        f"Read files: Use {_FILE_READ_TOOL_NAME} (NOT cat/head/tail)",
        f"Edit files: Use {_FILE_EDIT_TOOL_NAME} (NOT sed/awk)",
        f"Write files: Use {_FILE_WRITE_TOOL_NAME} (NOT echo >/cat <<EOF)",
        "Communication: Output text directly (NOT echo/printf)",
    ]

    avoid_commands = "`find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, or `echo`"

    multiple_commands_subitems = [
        (
            f"If the commands are independent and can run in parallel, make multiple "
            f"{BASH_TOOL_NAME} tool calls in a single message. Example: if you need to run "
            f'"git status" and "git diff", send a single message with two {BASH_TOOL_NAME} tool '
            "calls in parallel."
        ),
        (
            f"If the commands depend on each other and must run sequentially, use a single "
            f"{BASH_TOOL_NAME} call with '&&' to chain them together."
        ),
        "Use ';' only when you need to run commands sequentially but don't care if earlier "
        "commands fail.",
        "DO NOT use newlines to separate commands (newlines are ok in quoted strings).",
    ]

    git_subitems = [
        "Prefer to create a new commit rather than amending an existing commit.",
        (
            "Before running destructive operations (e.g., git reset --hard, git push --force, "
            "git checkout --), consider whether there is a safer alternative that achieves the "
            "same goal. Only use destructive operations when they are truly the best approach."
        ),
        (
            "Never skip hooks (--no-verify) or bypass signing (--no-gpg-sign, "
            "-c commit.gpgsign=false) unless the user has explicitly asked for it. If a hook "
            "fails, investigate and fix the underlying issue."
        ),
    ]

    sleep_subitems = [
        "Do not sleep between commands that can run immediately — just run them.",
        "If your command is long running and you would like to be notified when it finishes — "
        "use `run_in_background`. No sleep needed.",
        "Do not retry failing commands in a sleep loop — diagnose the root cause.",
        "If waiting for a background task you started with `run_in_background`, you will be "
        "notified when it completes — do not poll.",
        "If you must poll an external process, use a check command (e.g. `gh run view`) rather "
        "than sleeping first.",
        "If you must sleep, keep the duration short (1-5 seconds) to avoid blocking the user.",
    ]

    background_note = _get_background_usage_note()

    max_ms = get_max_timeout_ms()
    default_ms = get_default_timeout_ms()
    instruction_items: list[Any] = [
        "If your command will create new directories or files, first use this tool to run `ls` "
        "to verify the parent directory exists and is the correct location.",
        "Always quote file paths that contain spaces with double quotes in your command (e.g., "
        'cd "path with spaces/file.txt")',
        "Try to maintain your current working directory throughout the session by using absolute "
        "paths and avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.",
        (
            f"You may specify an optional timeout in milliseconds (up to {max_ms}ms / "
            f"{max_ms / 60000} minutes). By default, your command will timeout after "
            f"{default_ms}ms ({default_ms / 60000} minutes)."
        ),
        *([background_note] if background_note is not None else []),
        "When issuing multiple commands:",
        multiple_commands_subitems,
        "For git commands:",
        git_subitems,
        "Avoid unnecessary `sleep` commands:",
        sleep_subitems,
    ]

    return "\n".join(
        [
            "Executes a given bash command and returns its output.",
            "",
            "The working directory persists between commands, but shell state does not. The shell "
            "environment is initialized from the user's profile (bash or zsh).",
            "",
            (
                f"IMPORTANT: Avoid using this tool to run {avoid_commands} commands, unless "
                "explicitly instructed or after you have verified that a dedicated tool cannot "
                "accomplish your task. Instead, use the appropriate dedicated tool as this will "
                "provide a much better experience for the user:"
            ),
            "",
            *_prepend_bullets(tool_preference_items),
            (
                f"While the {BASH_TOOL_NAME} tool can do similar things, it’s better to use "
                "the built-in tools as they provide a better user experience and make it easier "
                "to review tool calls and give permission."
            ),
            "",
            "# Instructions",
            *_prepend_bullets(instruction_items),
            # getSimpleSandboxSection() -> '' when sandboxing is disabled (skeleton default).
            "",
            # getCommitAndPRInstructions() -> '' for external users without git instructions.
        ]
    )


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a pydantic model or a plain dict (the ``Out``/input may be either)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# Singleton instance (parity with the TS ``export const BashTool``).
bash_tool = BashTool()
