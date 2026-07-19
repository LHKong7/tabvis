"""sed allowlist/denylist validation.

Cross-cutting constraint check that blocks dangerous ``sed`` operations regardless of
permission mode: returns ``'passthrough'`` for non-sed / safe sed commands and ``'ask'``
for dangerous sed operations (w/W/e/E write/execute commands).

The allowlist patterns themselves are strict enough to reject dangerous operations:

  * Pattern 1 — line-printing (``sed -n 'N'`` / ``sed -n 'N,M'`` / ``sed -n '1p;2p'``)
    with optional ``-E``/``-r``/``-z`` flags. File arguments ALLOWED.
  * Pattern 2 — substitution (``sed 's/pattern/replacement/flags'``) restricted to
    ``g p i I m M`` plus optionally one digit. stdout-only unless ``allow_file_writes``.

Then a defense-in-depth denylist (:func:`contains_dangerous_operations`) rejects anything
that smells like a write/execute/transliterate trick even if the allowlist matched.

Casing: Python identifiers are snake_case; the returned :data:`PermissionResult` dict keeps
its Anthropic/transcript wire keys (``behavior``, ``message``, ``decisionReason``, ``reason``).
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from tabvis.types.permissions import PermissionResult, ToolPermissionContext
from tabvis.utils.bash.commands import split_command_deprecated
from tabvis.utils.bash.shell_quote import try_parse_shell_command

__all__ = [
    "is_line_printing_command",
    "is_print_command",
    "sed_command_is_allowed_by_allowlist",
    "has_file_args",
    "extract_sed_expressions",
    "check_sed_constraints",
]


class _SedOptions(TypedDict, total=False):
    allowFileWrites: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SED_PREFIX_RE = re.compile(r"^\s*sed\s+")


def _is_glob_token(token: Any) -> bool:
    """True when ``token`` is a shell-quote glob node (``{'op': 'glob', ...}``)."""
    return isinstance(token, dict) and token.get("op") == "glob"


def _validate_flags_against_allowlist(
    flags: list[str], allowed_flags: list[str]
) -> bool:
    """Validate flags against an allowlist.

    Handles both single flags and combined flags (e.g. ``-nE``). Returns ``True`` if all
    flags are valid, ``False`` otherwise.
    """
    for flag in flags:
        # Handle combined flags like -nE or -Er
        if flag.startswith("-") and not flag.startswith("--") and len(flag) > 2:
            # Check each character in combined flag
            for i in range(1, len(flag)):
                single_flag = "-" + flag[i]
                if single_flag not in allowed_flags:
                    return False
        else:
            # Single flag or long flag
            if flag not in allowed_flags:
                return False
    return True


def is_line_printing_command(command: str, expressions: list[str]) -> bool:
    """Pattern 1: line-printing command with ``-n`` flag.

    Allows ``sed -n 'N'`` / ``sed -n 'N,M'`` with optional ``-E``/``-r``/``-z`` flags and
    semicolon-separated print commands like ``sed -n '1p;2p;3p'``. File arguments ALLOWED.

    Exported for testing.
    """
    sed_match = _SED_PREFIX_RE.match(command)
    if not sed_match:
        return False

    without_sed = command[len(sed_match.group(0)) :]
    parse_result = try_parse_shell_command(without_sed)
    if not parse_result["success"]:
        return False
    parsed = parse_result["tokens"]

    # Extract all flags
    flags: list[str] = []
    for arg in parsed:
        if isinstance(arg, str) and arg.startswith("-") and arg != "--":
            flags.append(arg)

    # Validate flags - only allow -n, -E, -r, -z and their long forms
    allowed_flags = [
        "-n",
        "--quiet",
        "--silent",
        "-E",
        "--regexp-extended",
        "-r",
        "-z",
        "--zero-terminated",
        "--posix",
    ]

    if not _validate_flags_against_allowlist(flags, allowed_flags):
        return False

    # Check if -n flag is present (required for Pattern 1)
    has_n_flag = False
    for flag in flags:
        if flag in ("-n", "--quiet", "--silent"):
            has_n_flag = True
            break
        # Check in combined flags
        if flag.startswith("-") and not flag.startswith("--") and "n" in flag:
            has_n_flag = True
            break

    # Must have -n flag for Pattern 1
    if not has_n_flag:
        return False

    # Must have at least one expression
    if len(expressions) == 0:
        return False

    # All expressions must be print commands (strict allowlist)
    # Allow semicolon-separated commands
    for expr in expressions:
        commands = expr.split(";")
        for cmd in commands:
            if not is_print_command(cmd.strip()):
                return False

    return True


_PRINT_COMMAND_RE = re.compile(r"^(?:\d+|\d+,\d+)?p$")


def is_print_command(cmd: str) -> bool:
    """Check if a single command is a valid print command.

    STRICT ALLOWLIST — only these exact forms are allowed:
      * ``p`` (print all)
      * ``Np`` (print line N, where N is digits)
      * ``N,Mp`` (print lines N through M)
    Anything else (including w/W/e/E commands) is rejected.

    Exported for testing.
    """
    if not cmd:
        return False
    # ^(?:\d+|\d+,\d+)?p$ matches: p, 1p, 123p, 1,5p, 10,200p
    return bool(_PRINT_COMMAND_RE.match(cmd))


_SUBSTITUTION_PREFIX_RE = re.compile(r"^s\/(.*?)$", re.DOTALL)
_ALLOWED_SUBST_FLAG_CHARS_RE = re.compile(r"^[gpimIM]*[1-9]?[gpimIM]*$")


def _is_substitution_command(
    command: str,
    expressions: list[str],
    has_file_arguments: bool,
    options: _SedOptions | None = None,
) -> bool:
    """Pattern 2: substitution command.

    Allows ``sed 's/pattern/replacement/flags'`` where flags are only ``g p i I m M`` and
    optionally one digit 1-9. When ``allowFileWrites`` is true, allows the ``-i`` flag and
    file arguments for in-place editing; otherwise requires stdout-only.
    """
    allow_file_writes = bool((options or {}).get("allowFileWrites", False))

    # When not allowing file writes, must NOT have file arguments
    if not allow_file_writes and has_file_arguments:
        return False

    sed_match = _SED_PREFIX_RE.match(command)
    if not sed_match:
        return False

    without_sed = command[len(sed_match.group(0)) :]
    parse_result = try_parse_shell_command(without_sed)
    if not parse_result["success"]:
        return False
    parsed = parse_result["tokens"]

    # Extract all flags
    flags: list[str] = []
    for arg in parsed:
        if isinstance(arg, str) and arg.startswith("-") and arg != "--":
            flags.append(arg)

    # Validate flags based on mode. Base allowed flags for both modes.
    allowed_flags = ["-E", "--regexp-extended", "-r", "--posix"]

    # When allowing file writes, also permit -i and --in-place
    if allow_file_writes:
        allowed_flags.extend(["-i", "--in-place"])

    if not _validate_flags_against_allowlist(flags, allowed_flags):
        return False

    # Must have exactly one expression
    if len(expressions) != 1:
        return False

    expr = expressions[0].strip()

    # STRICT ALLOWLIST: Must be exactly a substitution command starting with 's'
    # This rejects standalone commands like 'e', 'w file', etc.
    if not expr.startswith("s"):
        return False

    # Parse substitution: s/pattern/replacement/flags. Only allow / as delimiter (strict).
    substitution_match = _SUBSTITUTION_PREFIX_RE.match(expr)
    if not substitution_match:
        return False

    rest = substitution_match.group(1)

    # Find the positions of / delimiters
    delimiter_count = 0
    last_delimiter_pos = -1
    i = 0
    while i < len(rest):
        if rest[i] == "\\":
            # Skip escaped character
            i += 2
            continue
        if rest[i] == "/":
            delimiter_count += 1
            last_delimiter_pos = i
        i += 1

    # Must have found exactly 2 delimiters (pattern and replacement)
    if delimiter_count != 2:
        return False

    # Extract flags (everything after the last delimiter)
    expr_flags = rest[last_delimiter_pos + 1 :]

    # Validate flags: only allow g, p, i, I, m, M, and optionally ONE digit 1-9
    if not _ALLOWED_SUBST_FLAG_CHARS_RE.match(expr_flags):
        return False

    return True


def sed_command_is_allowed_by_allowlist(
    command: str, options: _SedOptions | None = None
) -> bool:
    """Check if a sed command is allowed by the allowlist.

    The allowlist patterns themselves are strict enough to reject dangerous operations.

    ``options.allowFileWrites`` — when true, allows the ``-i`` flag and file arguments for
    substitution commands. Returns ``True`` if the command is allowed (matches allowlist and
    passes the denylist check), ``False`` otherwise.
    """
    allow_file_writes = bool((options or {}).get("allowFileWrites", False))

    # Extract sed expressions (content inside quotes where actual sed commands live)
    try:
        expressions = extract_sed_expressions(command)
    except Exception:  # noqa: BLE001 — parsing failed → not allowed
        return False

    # Check if sed command has file arguments
    has_file_arguments = has_file_args(command)

    # Check if command matches allowlist patterns
    is_pattern1 = False
    is_pattern2 = False

    if allow_file_writes:
        # When allowing file writes, only check substitution commands (Pattern 2 variant).
        # Pattern 1 (line printing) doesn't need file writes.
        is_pattern2 = _is_substitution_command(
            command, expressions, has_file_arguments, {"allowFileWrites": True}
        )
    else:
        # Standard read-only mode: check both patterns
        is_pattern1 = is_line_printing_command(command, expressions)
        is_pattern2 = _is_substitution_command(command, expressions, has_file_arguments)

    if not is_pattern1 and not is_pattern2:
        return False

    # Pattern 2 does not allow semicolons (command separators);
    # Pattern 1 allows semicolons for separating print commands.
    for expr in expressions:
        if is_pattern2 and ";" in expr:
            return False

    # Defense-in-depth: even if the allowlist matches, check the denylist.
    for expr in expressions:
        if _contains_dangerous_operations(expr):
            return False

    return True


def has_file_args(command: str) -> bool:
    """Check if a sed command has file arguments (not just stdin).

    Exported for testing.
    """
    sed_match = _SED_PREFIX_RE.match(command)
    if not sed_match:
        return False

    without_sed = command[len(sed_match.group(0)) :]
    parse_result = try_parse_shell_command(without_sed)
    if not parse_result["success"]:
        return True
    parsed = parse_result["tokens"]

    try:
        arg_count = 0
        has_e_flag = False

        i = 0
        while i < len(parsed):
            arg = parsed[i]

            # Handle both string arguments and glob patterns (like *.log)
            if not isinstance(arg, (str, dict)):
                i += 1
                continue

            # If it's a glob pattern, it counts as a file argument
            if _is_glob_token(arg):
                return True

            # Skip non-string arguments that aren't glob patterns
            if not isinstance(arg, str):
                i += 1
                continue

            # Handle -e flag followed by expression
            if arg in ("-e", "--expression") and i + 1 < len(parsed):
                has_e_flag = True
                i += 1  # Skip the next argument since it's the expression
                i += 1
                continue

            # Handle --expression=value format
            if arg.startswith("--expression="):
                has_e_flag = True
                i += 1
                continue

            # Handle -e=value format (non-standard but defense in depth)
            if arg.startswith("-e="):
                has_e_flag = True
                i += 1
                continue

            # Skip other flags
            if arg.startswith("-"):
                i += 1
                continue

            arg_count += 1

            # If we used -e flags, ALL non-flag arguments are file arguments
            if has_e_flag:
                return True

            # If we didn't use -e flags, the first non-flag argument is the sed expression,
            # so we need more than 1 non-flag argument to have file arguments
            if arg_count > 1:
                return True

            i += 1

        return False
    except Exception:  # noqa: BLE001
        return True  # Assume dangerous if parsing fails


_DANGEROUS_FLAG_EW_RE = re.compile(r"-e[wWe]")
_DANGEROUS_FLAG_WE_RE = re.compile(r"-w[eE]")


def extract_sed_expressions(command: str) -> list[str]:
    """Extract sed expressions from command, ignoring flags and filenames.

    Returns an array of sed expressions to check for dangerous operations. Raises
    :class:`ValueError` if parsing fails (or a dangerous flag combination is detected).

    Exported for testing.
    """
    expressions: list[str] = []

    # Calculate without_sed by trimming off the first N characters (removing 'sed ')
    sed_match = _SED_PREFIX_RE.match(command)
    if not sed_match:
        return expressions

    without_sed = command[len(sed_match.group(0)) :]

    # Reject dangerous flag combinations like -ew, -eW, -ee, -we (combined -e/-w with
    # dangerous commands).
    if _DANGEROUS_FLAG_EW_RE.search(without_sed) or _DANGEROUS_FLAG_WE_RE.search(
        without_sed
    ):
        raise ValueError("Dangerous flag combination detected")

    # Use shell-quote to parse the arguments properly
    parse_result = try_parse_shell_command(without_sed)
    if not parse_result["success"]:
        # Malformed shell syntax - raise error to be caught by caller
        raise ValueError(f"Malformed shell syntax: {parse_result.get('error')}")
    parsed = parse_result["tokens"]
    try:
        found_e_flag = False
        found_expression = False

        i = 0
        while i < len(parsed):
            arg = parsed[i]

            # Skip non-string arguments (like control operators)
            if not isinstance(arg, str):
                i += 1
                continue

            # Handle -e flag followed by expression
            if arg in ("-e", "--expression") and i + 1 < len(parsed):
                found_e_flag = True
                next_arg = parsed[i + 1]
                if isinstance(next_arg, str):
                    expressions.append(next_arg)
                    i += 1  # Skip the next argument since we consumed it
                i += 1
                continue

            # Handle --expression=value format
            if arg.startswith("--expression="):
                found_e_flag = True
                expressions.append(arg[len("--expression=") :])
                i += 1
                continue

            # Handle -e=value format (non-standard but defense in depth)
            if arg.startswith("-e="):
                found_e_flag = True
                expressions.append(arg[len("-e=") :])
                i += 1
                continue

            # Skip other flags
            if arg.startswith("-"):
                i += 1
                continue

            # If we haven't found any -e flags, the first non-flag argument is the sed
            # expression.
            if not found_e_flag and not found_expression:
                expressions.append(arg)
                found_expression = True
                i += 1
                continue

            # If we've already found -e flags or a standalone expression, remaining non-flag
            # arguments are filenames.
            break
    except Exception as error:  # noqa: BLE001
        # If shell-quote parsing fails, treat the sed command as unsafe.
        message = str(error) if str(error) else "Unknown error"
        raise ValueError(f"Failed to parse sed command: {message}") from error

    return expressions


# Precompiled denylist patterns used by _contains_dangerous_operations.
_NON_ASCII_RE = re.compile(r"[^\x01-\x7F]")
_NEGATION_START_RE = re.compile(r"^!")
_NEGATION_AFTER_RE = re.compile(r"[/\d$]!")
_TILDE_STEP_RE = re.compile(r"\d\s*~\s*\d|,\s*~\s*\d|\$\s*~\s*\d")
_COMMA_START_RE = re.compile(r"^,")
_COMMA_OFFSET_RE = re.compile(r",\s*[+-]")
_BACKSLASH_S_RE = re.compile(r"s\\")
_BACKSLASH_DELIM_RE = re.compile(r"\\[|#%@]")
_ESCAPED_SLASH_W_RE = re.compile(r"\\\/.*[wW]")
_SLASH_DANGEROUS_RE = re.compile(r"\/[^/]*\s+[wWeE]")
_MALFORMED_SUBST_PREFIX_RE = re.compile(r"^s\/")
_WELL_FORMED_SUBST_RE = re.compile(r"^s\/[^/]*\/[^/]*\/[^/]*$")
_S_DOT_RE = re.compile(r"^s.")
_ENDS_DANGEROUS_RE = re.compile(r"[wWeE]$")
_PROPER_SUBST_RE = re.compile(r"^s([^\\\n]).*?\1.*?\1[^wWeE]*$")
_Y_COMMAND_RE = re.compile(r"y([^\\\n])")
_ANY_DANGEROUS_CHAR_RE = re.compile(r"[wWeE]")
_SUBST_FLAGS_RE = re.compile(r"s([^\\\n]).*?\1.*?\1(.*?)$")

_WRITE_COMMAND_REGEXES = [
    re.compile(r"^[wW]\s*\S+"),  # At start: w file
    re.compile(r"^\d+\s*[wW]\s*\S+"),  # After line number: 1w file or 1 w file
    re.compile(r"^\$\s*[wW]\s*\S+"),  # After $: $w file or $ w file
    re.compile(r"^\/[^/]*\/[IMim]*\s*[wW]\s*\S+"),  # After pattern: /pattern/w file
    re.compile(r"^\d+,\d+\s*[wW]\s*\S+"),  # After range: 1,10w file
    re.compile(r"^\d+,\$\s*[wW]\s*\S+"),  # After range: 1,$w file
    # After pattern range: /s/,/e/w file
    re.compile(r"^\/[^/]*\/[IMim]*,\/[^/]*\/[IMim]*\s*[wW]\s*\S+"),
]

_EXECUTE_COMMAND_REGEXES = [
    re.compile(r"^e"),  # At start: e cmd
    re.compile(r"^\d+\s*e"),  # After line number: 1e or 1 e
    re.compile(r"^\$\s*e"),  # After $: $e or $ e
    re.compile(r"^\/[^/]*\/[IMim]*\s*e"),  # After pattern: /pattern/e
    re.compile(r"^\d+,\d+\s*e"),  # After range: 1,10e
    re.compile(r"^\d+,\$\s*e"),  # After range: 1,$e
    # After pattern range: /s/,/e/e
    re.compile(r"^\/[^/]*\/[IMim]*,\/[^/]*\/[IMim]*\s*e"),
]


def _contains_dangerous_operations(expression: str) -> bool:
    """Check if a sed expression contains dangerous operations (denylist).

    ``expression`` is a single sed expression (without quotes). Returns ``True`` if
    dangerous, ``False`` if safe. CONSERVATIVE: when in doubt, treat as unsafe.
    """
    cmd = expression.strip()
    if not cmd:
        return False

    # Reject non-ASCII characters (Unicode homoglyphs, combining chars, etc.).
    # Check for characters outside the ASCII range (0x01-0x7F, excluding null byte).
    if _NON_ASCII_RE.search(cmd):
        return True

    # Reject curly braces (blocks) - too complex to parse
    if "{" in cmd or "}" in cmd:
        return True

    # Reject newlines - multi-line commands are too complex
    if "\n" in cmd:
        return True

    # Reject comments (# not immediately after s command).
    # Comments look like: #comment or start with #. Delimiter looks like: s#pattern#replacement#
    hash_index = cmd.find("#")
    if hash_index != -1 and not (hash_index > 0 and cmd[hash_index - 1] == "s"):
        return True

    # Reject negation operator. Negation can appear: at start (!/pattern/), after address
    # (/pattern/!, 1,10!, $!). Delimiter looks like: s!pattern!replacement! (has 's' before).
    if _NEGATION_START_RE.search(cmd) or _NEGATION_AFTER_RE.search(cmd):
        return True

    # Reject tilde in GNU step address format (digit~digit, ,~digit, or $~digit).
    if _TILDE_STEP_RE.search(cmd):
        return True

    # Reject comma at start (bare comma is shorthand for 1,$ address range).
    if _COMMA_START_RE.search(cmd):
        return True

    # Reject comma followed by +/- (GNU offset addresses).
    if _COMMA_OFFSET_RE.search(cmd):
        return True

    # Reject backslash tricks:
    # 1. s\ (substitution with backslash delimiter)
    # 2. \X where X could be an alternate delimiter (|, #, %, etc.) - not regex escapes
    if _BACKSLASH_S_RE.search(cmd) or _BACKSLASH_DELIM_RE.search(cmd):
        return True

    # Reject escaped slashes followed by w/W (patterns like /\/path\/to\/file/w).
    if _ESCAPED_SLASH_W_RE.search(cmd):
        return True

    # Reject malformed/suspicious patterns we don't understand. If there's a slash followed
    # by non-slash chars, then whitespace, then dangerous commands.
    if _SLASH_DANGEROUS_RE.search(cmd):
        return True

    # Reject malformed substitution commands that don't follow the normal pattern.
    # Examples: s/foobareoutput.txt (missing delimiters), s/foo/bar//w (extra delimiter).
    if _MALFORMED_SUBST_PREFIX_RE.search(cmd) and not _WELL_FORMED_SUBST_RE.search(cmd):
        return True

    # PARANOID: Reject any command starting with 's' that ends with dangerous chars
    # (w, W, e, E) and doesn't match our known safe substitution pattern.
    if _S_DOT_RE.search(cmd) and _ENDS_DANGEROUS_RE.search(cmd):
        # Check if it's a properly formed substitution (any delimiter, not just /).
        if not _PROPER_SUBST_RE.search(cmd):
            return True

    # Check for dangerous write commands.
    if any(rx.search(cmd) for rx in _WRITE_COMMAND_REGEXES):
        return True

    # Check for dangerous execute commands.
    if any(rx.search(cmd) for rx in _EXECUTE_COMMAND_REGEXES):
        return True

    # Check for substitution commands with dangerous flags.
    # Pattern: s<delim>pattern<delim>replacement<delim>flags where flags contain w or e.
    substitution_match = _SUBST_FLAGS_RE.search(cmd)
    if substitution_match:
        flags = substitution_match.group(2) or ""

        # Check for write flag: s/old/new/w filename or s/old/new/gw filename
        if "w" in flags or "W" in flags:
            return True

        # Check for execute flag: s/old/new/e or s/old/new/ge
        if "e" in flags or "E" in flags:
            return True

    # Check for y (transliterate) command followed by dangerous operations.
    # PARANOID: reject any y command that has w/W/e/E anywhere after the delimiters.
    y_command_match = _Y_COMMAND_RE.search(cmd)
    if y_command_match:
        # If we see a y command, check if there's any w, W, e, or E in the entire command.
        if _ANY_DANGEROUS_CHAR_RE.search(cmd):
            return True

    return False


def check_sed_constraints(
    input: dict[str, Any], tool_permission_context: ToolPermissionContext
) -> PermissionResult:
    """Cross-cutting validation step for sed commands.

    A constraint check that blocks dangerous sed operations regardless of mode. Returns
    ``'passthrough'`` for non-sed commands or safe sed commands, and ``'ask'`` for dangerous
    sed operations (w/W/e/E commands).

      * ``'ask'`` — if any sed command contains dangerous operations
      * ``'passthrough'`` — if there are no sed commands or all are safe
    """
    commands = split_command_deprecated(input["command"])

    for cmd in commands:
        # Skip non-sed commands
        trimmed = cmd.strip()
        parts = re.split(r"\s+", trimmed)
        base_cmd = parts[0] if parts else ""
        if base_cmd != "sed":
            continue

        # In acceptEdits mode, allow file writes (-i flag) but still block dangerous ops.
        allow_file_writes = tool_permission_context.get("mode") == "acceptEdits"

        is_allowed = sed_command_is_allowed_by_allowlist(
            trimmed, {"allowFileWrites": allow_file_writes}
        )

        if not is_allowed:
            return {
                "behavior": "ask",
                "message": (
                    "sed command requires approval (contains potentially dangerous "
                    "operations)"
                ),
                "decisionReason": {
                    "type": "other",
                    "reason": (
                        "sed command contains operations that require explicit approval "
                        "(e.g., write commands, execute commands)"
                    ),
                },
            }

    # No dangerous sed commands found (or no sed commands at all)
    return {
        "behavior": "passthrough",
        "message": "No dangerous sed operations detected",
    }
