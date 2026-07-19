"""AST-based bash command analysis using tree-sitter

This module is the security tree-walk consumed by ``BashTool`` / ``bashPermissions``. It
parses a bash command with tree-sitter-bash and walks the tree with an EXPLICIT allowlist of
node types. Any node type not in the allowlist classifies the entire command as
``'too-complex'`` (→ permission prompt). The key design property is FAIL-CLOSED: we never
interpret structure we don't understand.

RUNTIME NOTE (memory ``bash-parser-gated-off``): :func:`parse_command_raw` (from
:mod:`tabvis.utils.bash.parser`) is gated off and **always returns ``None``** at runtime, so
:func:`parse_for_security` always yields the ``'parse-unavailable'`` path. The full walker is
implemented with equivalent behavior anyway — it is exercised when a root is supplied via
:func:`parse_for_security_from_ast`.

AST node shape (``TsNode``): ``{type, text, startIndex (UTF-8 byte offset),
endIndex (UTF-8 byte offset), children}``. Where the TS tracks index *gaps* (``walkString``)
this implementation tracks the same ``startIndex``/``endIndex`` byte offsets verbatim.

``ParseForSecurityResult`` / ``SemanticCheckResult`` are union *dicts* keyed on ``kind`` /
``ok`` (wire keys preserved: ``kind``, ``reason``, ``nodeType``, ``commands``, ``ok``).
:class:`SimpleCommand` / :class:`Redirect` are dataclasses with the TS field names verbatim
(``argv``, ``envVars``, ``redirects``, ``text``, ``op``, ``target``, ``fd``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tabvis.utils.bash.bash_parser import SHELL_KEYWORDS
from tabvis.utils.bash.parser import PARSE_ABORTED, Node, parse_command_raw

# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class Redirect:
    """Field names kept verbatim."""

    op: str  # '>' | '>>' | '<' | '<<' | '>&' | '>|' | '<&' | '&>' | '&>>' | '<<<'
    target: str
    fd: int | None = None


@dataclass
class EnvVar:
    """A leading ``VAR=val`` assignment (TS ``{ name, value }``)."""

    name: str
    value: str


@dataclass
class SimpleCommand:
    """Field names kept verbatim."""

    # argv[0] is the command name, rest are arguments with quotes already resolved.
    argv: list[str] = field(default_factory=list)
    # Leading VAR=val assignments. Wire-key kept verbatim (TS ``SimpleCommand.envVars``).
    envVars: list[EnvVar] = field(default_factory=list)  # noqa: N815
    # Output/input redirects.
    redirects: list[Redirect] = field(default_factory=list)
    # Original source span for this command (for UI display).
    text: str = ""


# ``ParseForSecurityResult`` is a union dict keyed on ``kind``:
#   {'kind': 'simple', 'commands': list[SimpleCommand]}
#   {'kind': 'too-complex', 'reason': str, 'nodeType'?: str}
#   {'kind': 'parse-unavailable'}
ParseForSecurityResult = dict

# ``SemanticCheckResult`` is a union dict keyed on ``ok``:
#   {'ok': True}
#   {'ok': False, 'reason': str}
SemanticCheckResult = dict


# ── Constant sets / regexes ──────────────────────────────────────────────────

# Structural node types that represent composition of commands. We recurse through these to
# find the leaf `command` nodes. `program` is the root; `list` is `a && b || c`; `pipeline`
# is `a | b`; `redirected_statement` wraps a command with its redirects.
STRUCTURAL_TYPES = {
    "program",
    "list",
    "pipeline",
    "redirected_statement",
}

# Operator tokens that separate commands. Leaf nodes between commands in
# `list`/`pipeline`/`program` that carry no payload.
SEPARATOR_TYPES = {"&&", "||", "|", ";", "&", "|&", "\n"}

# Placeholder string used in outer argv when a $() is recursively extracted.
CMDSUB_PLACEHOLDER = "__CMDSUB_OUTPUT__"

# Placeholder for simple_expansion ($VAR) references to vars set earlier in the same command.
VAR_PLACEHOLDER = "__TRACKED_VAR__"


def contains_any_placeholder(value: str) -> bool:
    """Defense-in-depth substring check."""
    return CMDSUB_PLACEHOLDER in value or VAR_PLACEHOLDER in value


# Unquoted $VAR undergoes word-splitting (on default $IFS) + pathname expansion (glob).
BARE_VAR_UNSAFE_RE = re.compile(r"[ \t\n*?[]")

# stdbuf flag forms — hoisted from the wrapper-stripping while-loop.
STDBUF_SHORT_SEP_RE = re.compile(r"^-[ioe]$")
STDBUF_SHORT_FUSED_RE = re.compile(r"^-[ioe].")
STDBUF_LONG_RE = re.compile(r"^--(input|output|error)=")

# Known-safe environment variables bash sets automatically.
SAFE_ENV_VARS = {
    "HOME",
    "PWD",
    "OLDPWD",
    "USER",
    "LOGNAME",
    "SHELL",
    "PATH",
    "HOSTNAME",
    "UID",
    "EUID",
    "PPID",
    "RANDOM",
    "SECONDS",
    "LINENO",
    "TMPDIR",
    "BASH_VERSION",
    "BASHPID",
    "SHLVL",
    "HISTFILE",
    "IFS",
}

# Special shell variables ($?, $$, $!, $#, $0-$9). Note '@' and '*' are intentionally absent.
SPECIAL_VAR_NAMES = {
    "?",
    "$",
    "!",
    "#",
    "0",
    "-",
}

# Node types that mean "this command cannot be statically analyzed."
DANGEROUS_TYPES = [
    "command_substitution",
    "process_substitution",
    "expansion",
    "simple_expansion",
    "brace_expression",
    "subshell",
    "compound_statement",
    "for_statement",
    "while_statement",
    "until_statement",
    "if_statement",
    "case_statement",
    "function_definition",
    "test_command",
    "ansi_c_string",
    "translated_string",
    "herestring_redirect",
    "heredoc_redirect",
]
_DANGEROUS_TYPES_SET = set(DANGEROUS_TYPES)

# Numeric IDs for analytics (logEvent doesn't accept strings). Index into DANGEROUS_TYPES.
DANGEROUS_TYPE_IDS = list(DANGEROUS_TYPES)


def node_type_id(node_type: str | None) -> int:
    """0 = Unknown/other, -1 = ERROR, -2 = pre-check."""
    if not node_type:
        return -2
    if node_type == "ERROR":
        return -1
    try:
        i = DANGEROUS_TYPE_IDS.index(node_type)
    except ValueError:
        return 0
    return i + 1


# Redirect operator tokens → canonical operator.
REDIRECT_OPS = {
    ">": ">",
    ">>": ">>",
    "<": "<",
    ">&": ">&",
    "<&": "<&",
    ">|": ">|",
    "&>": "&>",
    "&>>": "&>>",
    "<<<": "<<<",
}

# Brace expansion pattern: {a,b} or {a..b}.
BRACE_EXPANSION_RE = re.compile(r"\{[^{}\s]*(,|\.\.)[^{}\s]*\}")

# Control characters bash silently drops but confuse static analysis (incl. CR 0x0D).
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Unicode whitespace beyond ASCII (NBSP, zero-width, line/paragraph separators, BOM).
UNICODE_WHITESPACE_RE = re.compile(
    "[\u00A0\u1680\u2000-\u200B\u2028\u2029\u202F\u205F\u3000\uFEFF]"
)

# Backslash immediately before whitespace, or before a newline adjacent to a non-ws char.
BACKSLASH_WHITESPACE_RE = re.compile(r"\\[ \t]|[^ \t\n\\]\\\n")

# Zsh dynamic named directory expansion: ~[name].
ZSH_TILDE_BRACKET_RE = re.compile(r"~\[")

# Zsh EQUALS expansion: word-initial `=cmd`.
ZSH_EQUALS_EXPANSION_RE = re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]")

# Brace character combined with quote characters (expansion obfuscation).
BRACE_WITH_QUOTE_RE = re.compile(r"\{[^}]*['\"]")


def mask_braces_in_quoted_contexts(cmd: str) -> str:
    """Mask ``{`` inside quoted spans.

    Single-pass bash-aware quote-state scanner. ``{`` inside single/double quotes is replaced
    with a space (brace expansion is impossible in either quote context).
    """
    # Fast path: no `{` → nothing to mask.
    if "{" not in cmd:
        return cmd
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if in_single:
            # Bash single quotes: no escapes, `'` always terminates.
            if c == "'":
                in_single = False
            out.append(" " if c == "{" else c)
            i += 1
        elif in_double:
            # Bash double quotes: `\` escapes `"` and `\`.
            if c == "\\" and i + 1 < n and (cmd[i + 1] == '"' or cmd[i + 1] == "\\"):
                out.append(c)
                out.append(cmd[i + 1])
                i += 2
            else:
                if c == '"':
                    in_double = False
                out.append(" " if c == "{" else c)
                i += 1
        else:
            # Unquoted: `\` escapes any next char.
            if c == "\\" and i + 1 < n:
                out.append(c)
                out.append(cmd[i + 1])
                i += 2
            else:
                if c == "'":
                    in_single = True
                elif c == '"':
                    in_double = True
                out.append(c)
                i += 1
    return "".join(out)


DOLLAR = chr(0x24)

# Detect a resolved simple_expansion in node.text (for the walkCommand .text rebuild).
_DOLLAR_IDENT_RE = re.compile(r"\$[A-Za-z_]")
# Chars that force shell-escaping of an argv element when rebuilding .text.
_ARG_NEEDS_QUOTE_RE = re.compile(r"[\"'\\ \t\n$`;|&<>(){}*?[\]~#]")


# ── Entry points ─────────────────────────────────────────────────────────────


async def parse_for_security(cmd: str) -> ParseForSecurityResult:
    """Parse the for security.

    RUNTIME-GATED-OFF: ``parse_command_raw`` always returns ``None``, so non-empty commands
    yield ``{'kind': 'parse-unavailable'}``. The empty-string short-circuit still returns a
    simple (empty) result.
    """
    # parseCommandRaw('') returns null (falsy check), so short-circuit here. Don't use .strip()
    # — it strips Unicode whitespace which the pre-checks need to see and reject.
    if cmd == "":
        return {"kind": "simple", "commands": []}
    root = await parse_command_raw(cmd)
    if root is None:
        return {"kind": "parse-unavailable"}
    return parse_for_security_from_ast(cmd, root)


def parse_for_security_from_ast(cmd: str, root) -> ParseForSecurityResult:
    """Parse the for security from ast.

    Pre-checks (tree-sitter/bash differentials) run on ``cmd`` first; then the tree walk runs
    on ``root`` (a :data:`Node` or the :data:`PARSE_ABORTED` sentinel).
    """
    if CONTROL_CHAR_RE.search(cmd):
        return {"kind": "too-complex", "reason": "Contains control characters"}
    if UNICODE_WHITESPACE_RE.search(cmd):
        return {"kind": "too-complex", "reason": "Contains Unicode whitespace"}
    if BACKSLASH_WHITESPACE_RE.search(cmd):
        return {
            "kind": "too-complex",
            "reason": "Contains backslash-escaped whitespace",
        }
    if ZSH_TILDE_BRACKET_RE.search(cmd):
        return {
            "kind": "too-complex",
            "reason": "Contains zsh ~[ dynamic directory syntax",
        }
    if ZSH_EQUALS_EXPANSION_RE.search(cmd):
        return {
            "kind": "too-complex",
            "reason": "Contains zsh =cmd equals expansion",
        }
    if BRACE_WITH_QUOTE_RE.search(mask_braces_in_quoted_contexts(cmd)):
        return {
            "kind": "too-complex",
            "reason": "Contains brace with quote character (expansion obfuscation)",
        }

    trimmed = cmd.strip()
    if trimmed == "":
        return {"kind": "simple", "commands": []}

    if root is PARSE_ABORTED:
        # Module loaded but parse aborted (timeout / node budget / panic). Fail closed.
        return {
            "kind": "too-complex",
            "reason": "Parser aborted (timeout or resource limit) — possible adversarial input",
            "nodeType": "PARSE_ABORT",
        }

    return walk_program(root)


def walk_program(root: Node) -> ParseForSecurityResult:
    """Collect commands from the root, tracking var scope."""
    commands: list[SimpleCommand] = []
    var_scope: dict[str, str] = {}
    err = collect_commands(root, commands, var_scope)
    if err:
        return err
    return {"kind": "simple", "commands": commands}


# ── Tree walkers ─────────────────────────────────────────────────────────────


def collect_commands(
    node: Node,
    commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult | None:
    """Recursively collect leaf ``command`` nodes."""
    ntype = node["type"]

    if ntype == "command":
        result = walk_command(node, [], commands, var_scope)
        if result["kind"] != "simple":
            return result
        commands.extend(result["commands"])
        return None

    if ntype == "redirected_statement":
        return walk_redirected_statement(node, commands, var_scope)

    if ntype == "comment":
        return None

    if ntype in STRUCTURAL_TYPES:
        is_pipeline = ntype == "pipeline"
        needs_snapshot = False
        if not is_pipeline:
            for c in node["children"]:
                if c and (c["type"] == "||" or c["type"] == "&"):
                    needs_snapshot = True
                    break
        snapshot = dict(var_scope) if needs_snapshot else None
        # For pipeline, ALL stages run in subshells — start with a copy. For list/program the
        # &&/; chain mutates caller's scope; fork only on ||/&.
        scope = dict(var_scope) if is_pipeline else var_scope
        for child in node["children"]:
            if not child:
                continue
            if child["type"] in SEPARATOR_TYPES:
                if child["type"] in ("||", "|", "|&", "&"):
                    scope = dict(snapshot if snapshot is not None else var_scope)
                continue
            err = collect_commands(child, commands, scope)
            if err:
                return err
        return None

    if ntype == "negated_command":
        # `! cmd` inverts exit code only. Recurse into the wrapped command.
        for child in node["children"]:
            if not child:
                continue
            if child["type"] == "!":
                continue
            return collect_commands(child, commands, var_scope)
        return None

    if ntype == "declaration_command":
        # export/local/readonly/declare/typeset.
        argv: list[str] = []
        for child in node["children"]:
            if not child:
                continue
            ctype = child["type"]
            if ctype in ("export", "local", "readonly", "declare", "typeset"):
                argv.append(child["text"])
            elif ctype in ("word", "number", "raw_string", "string", "concatenation"):
                arg = walk_argument(child, commands, var_scope)
                if not isinstance(arg, str):
                    return arg
                # declare/typeset/local flags that change assignment semantics.
                if (
                    argv and argv[0] in ("declare", "typeset", "local")
                ) and re.match(r"^-[a-zA-Z]*[niaA]", arg):
                    return {
                        "kind": "too-complex",
                        "reason": (
                            f"declare flag {arg} changes assignment semantics "
                            "(nameref/integer/array)"
                        ),
                        "nodeType": "declaration_command",
                    }
                # bare positional assignment with a subscript also evaluates.
                if (
                    (argv and argv[0] in ("declare", "typeset", "local"))
                    and arg[0:1] != "-"
                    and re.match(r"^[^=]*\[", arg)
                ):
                    return {
                        "kind": "too-complex",
                        "reason": (
                            f"declare positional '{arg}' contains array subscript — "
                            "bash evaluates $(cmd) in subscripts"
                        ),
                        "nodeType": "declaration_command",
                    }
                argv.append(arg)
            elif ctype == "variable_assignment":
                ev = walk_variable_assignment(child, commands, var_scope)
                if isinstance(ev, dict) and "kind" in ev:
                    return ev
                apply_var_to_scope(var_scope, ev)
                argv.append(f"{ev['name']}={ev['value']}")
            elif ctype == "variable_name":
                # `export FOO` — bare name, no assignment.
                argv.append(child["text"])
            else:
                return too_complex(child)
        commands.append(
            SimpleCommand(argv=argv, envVars=[], redirects=[], text=node["text"])
        )
        return None

    if ntype == "variable_assignment":
        # Bare `VAR=value` at statement level (inert — no command pushed).
        ev = walk_variable_assignment(node, commands, var_scope)
        if isinstance(ev, dict) and "kind" in ev:
            return ev
        apply_var_to_scope(var_scope, ev)
        return None

    if ntype == "for_statement":
        return _collect_for_statement(node, commands, var_scope)

    if ntype in ("if_statement", "while_statement"):
        return _collect_if_while(node, commands, var_scope)

    if ntype == "subshell":
        # `(cmd1; cmd2)` — isolated scope; use a COPY.
        inner_scope = dict(var_scope)
        for child in node["children"]:
            if not child:
                continue
            if child["type"] in ("(", ")"):
                continue
            err = collect_commands(child, commands, inner_scope)
            if err:
                return err
        return None

    if ntype == "test_command":
        # `[[ EXPR ]]` / `[ EXPR ]`.
        argv = ["[["]
        for child in node["children"]:
            if not child:
                continue
            if child["type"] in ("[[", "]]", "[", "]"):
                continue
            err = walk_test_expr(child, argv, commands, var_scope)
            if err:
                return err
        commands.append(
            SimpleCommand(argv=argv, envVars=[], redirects=[], text=node["text"])
        )
        return None

    if ntype == "unset_command":
        # `unset FOO BAR`, `unset -f func`.
        argv = []
        for child in node["children"]:
            if not child:
                continue
            ctype = child["type"]
            if ctype == "unset":
                argv.append(child["text"])
            elif ctype == "variable_name":
                argv.append(child["text"])
                # unset removes the var from scope.
                var_scope.pop(child["text"], None)
            elif ctype == "word":
                arg = walk_argument(child, commands, var_scope)
                if not isinstance(arg, str):
                    return arg
                argv.append(arg)
            else:
                return too_complex(child)
        commands.append(
            SimpleCommand(argv=argv, envVars=[], redirects=[], text=node["text"])
        )
        return None

    return too_complex(node)


def _collect_for_statement(
    node: Node,
    commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult | None:
    """Collect commands from the statement node."""
    loop_var: str | None = None
    do_group: Node | None = None
    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]
        if ctype == "variable_name":
            loop_var = child["text"]
        elif ctype == "do_group":
            do_group = child
        elif ctype in ("for", "in", "select", ";"):
            continue  # structural tokens
        elif ctype == "command_substitution":
            # `for i in $(seq 1 3)` — inner cmd IS extracted and rule-checked.
            err = collect_command_substitution(child, commands, var_scope)
            if err:
                return err
        else:
            # Iteration values — validated; value discarded.
            arg = walk_argument(child, commands, var_scope)
            if not isinstance(arg, str):
                return arg
    if loop_var is None or do_group is None:
        return too_complex(node)
    if loop_var in ("PS4", "IFS"):
        return {
            "kind": "too-complex",
            "reason": f"{loop_var} as loop variable bypasses assignment validation",
            "nodeType": "for_statement",
        }
    # Loop var set in REAL scope (still set after loop in bash); ALWAYS VAR_PLACEHOLDER.
    var_scope[loop_var] = VAR_PLACEHOLDER
    body_scope = dict(var_scope)
    for c in do_group["children"]:
        if not c:
            continue
        if c["type"] in ("do", "done", ";"):
            continue
        err = collect_commands(c, commands, body_scope)
        if err:
            return err
    return None


def _collect_if_while(
    node: Node,
    commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult | None:
    """Collect commands from the statement node."""
    seen_then = False
    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]
        if ctype in ("if", "fi", "else", "elif", "while", "until", ";"):
            continue
        if ctype == "then":
            seen_then = True
            continue
        if ctype == "do_group":
            body_scope = dict(var_scope)
            for c in child["children"]:
                if not c:
                    continue
                if c["type"] in ("do", "done", ";"):
                    continue
                err = collect_commands(c, commands, body_scope)
                if err:
                    return err
            continue
        if ctype in ("elif_clause", "else_clause"):
            branch_scope = dict(var_scope)
            for c in child["children"]:
                if not c:
                    continue
                if c["type"] in ("elif", "else", "then", ";"):
                    continue
                err = collect_commands(c, commands, branch_scope)
                if err:
                    return err
            continue
        # Condition (seen_then False) uses REAL scope; then-body (True) uses a COPY.
        target_scope = dict(var_scope) if seen_then else var_scope
        before = len(commands)
        err = collect_commands(child, commands, target_scope)
        if err:
            return err
        if not seen_then:
            for i in range(before, len(commands)):
                c = commands[i]
                if c and c.argv and c.argv[0] == "read":
                    for a in c.argv[1:]:
                        if not a.startswith("-") and re.match(
                            r"^[A-Za-z_][A-Za-z0-9_]*$", a
                        ):
                            existing = var_scope.get(a)
                            if existing is not None and not contains_any_placeholder(
                                existing
                            ):
                                return {
                                    "kind": "too-complex",
                                    "reason": (
                                        f"'read {a}' in condition may not execute "
                                        "(||/pipeline/subshell); cannot prove it overwrites "
                                        f"tracked literal '{existing}'"
                                    ),
                                    "nodeType": "if_statement",
                                }
                            var_scope[a] = VAR_PLACEHOLDER
    return None


def walk_test_expr(
    node: Node,
    argv: list[str],
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult | None:
    """Recurse a ``test_command`` expression tree."""
    ntype = node["type"]
    if ntype in (
        "unary_expression",
        "binary_expression",
        "negated_expression",
        "parenthesized_expression",
    ):
        for c in node["children"]:
            if not c:
                continue
            err = walk_test_expr(c, argv, inner_commands, var_scope)
            if err:
                return err
        return None
    if ntype in (
        "test_operator",
        "!",
        "(",
        ")",
        "&&",
        "||",
        "==",
        "=",
        "!=",
        "<",
        ">",
        "=~",
    ):
        argv.append(node["text"])
        return None
    if ntype in ("regex", "extglob_pattern"):
        # RHS of =~ or ==/!= in [[ ]]. Pattern text only.
        argv.append(node["text"])
        return None
    # Operand — word, string, number, etc. Validate via walk_argument.
    arg = walk_argument(node, inner_commands, var_scope)
    if not isinstance(arg, str):
        return arg
    argv.append(arg)
    return None


def walk_redirected_statement(
    node: Node,
    commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult | None:
    """Collect commands and redirects from a redirected statement."""
    redirects: list[Redirect] = []
    inner_command: Node | None = None

    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]
        if ctype == "file_redirect":
            r = walk_file_redirect(child, commands, var_scope)
            if isinstance(r, dict) and "kind" in r:
                return r
            redirects.append(r)
        elif ctype == "heredoc_redirect":
            r = walk_heredoc_redirect(child)
            if r:
                return r
        elif ctype in (
            "command",
            "pipeline",
            "list",
            "negated_command",
            "declaration_command",
            "unset_command",
        ):
            inner_command = child
        else:
            return too_complex(child)

    if not inner_command:
        # `> file` alone is valid bash (truncates file).
        commands.append(
            SimpleCommand(argv=[], envVars=[], redirects=redirects, text=node["text"])
        )
        return None

    before = len(commands)
    err = collect_commands(inner_command, commands, var_scope)
    if err:
        return err
    if len(commands) > before and len(redirects) > 0:
        last = commands[-1]
        if last:
            last.redirects.extend(redirects)
    return None


def walk_file_redirect(
    node: Node,
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> Redirect | ParseForSecurityResult:
    """Extract op + target from a ``file_redirect`` node."""
    op: str | None = None
    target: str | None = None
    fd: int | None = None

    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]
        if ctype == "file_descriptor":
            try:
                fd = int(child["text"])
            except ValueError:
                fd = None
        elif ctype in REDIRECT_OPS:
            op = REDIRECT_OPS.get(ctype)
        elif ctype in ("word", "number"):
            # number/word nodes with children smuggle expansions (NN# base syntax).
            if len(child["children"]) > 0:
                return too_complex(child)
            if BRACE_EXPANSION_RE.search(child["text"]):
                return too_complex(child)
            # Unescape backslash sequences — bash quote removal turns `\X` → `X`.
            target = re.sub(r"\\(.)", r"\1", child["text"])
        elif ctype == "raw_string":
            target = strip_literal_text(child["text"])
        elif ctype == "string":
            s = walk_string(child, inner_commands, var_scope)
            if not isinstance(s, str):
                return s
            target = s
        elif ctype == "concatenation":
            s = walk_argument(child, inner_commands, var_scope)
            if not isinstance(s, str):
                return s
            target = s
        else:
            return too_complex(child)

    if not op or target is None:
        return {
            "kind": "too-complex",
            "reason": "Unrecognized redirect shape",
            "nodeType": node["type"],
        }
    return Redirect(op=op, target=target, fd=fd)


def walk_heredoc_redirect(node: Node) -> ParseForSecurityResult | None:
    """Only quoted-delimiter heredocs are safe."""
    start_text: str | None = None
    body: Node | None = None

    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]
        if ctype == "heredoc_start":
            start_text = child["text"]
        elif ctype == "heredoc_body":
            body = child
        elif ctype in ("<<", "<<-", "heredoc_end", "file_descriptor"):
            # expected structural tokens — safe to skip.
            pass
        else:
            # pipeline/command/redirect following the delimiter on the same line.
            return too_complex(child)

    is_quoted = start_text is not None and (
        (start_text.startswith("'") and start_text.endswith("'"))
        or (start_text.startswith('"') and start_text.endswith('"'))
        or start_text.startswith("\\")
    )

    if not is_quoted:
        return {
            "kind": "too-complex",
            "reason": "Heredoc with unquoted delimiter undergoes shell expansion",
            "nodeType": "heredoc_redirect",
        }

    if body:
        for child in body["children"]:
            if not child:
                continue
            if child["type"] != "heredoc_content":
                return too_complex(child)
    return None


def walk_herestring_redirect(
    node: Node,
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult | None:
    """Collect the redirect and content of a here-string."""
    for child in node["children"]:
        if not child:
            continue
        if child["type"] == "<<<":
            continue
        content = walk_argument(child, inner_commands, var_scope)
        if not isinstance(content, str):
            return content
        if NEWLINE_HASH_RE.search(content):
            return too_complex(child)
    return None


def walk_command(
    node: Node,
    extra_redirects: list[Redirect],
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult:
    """Extract argv from a ``command`` node."""
    argv: list[str] = []
    env_vars: list[EnvVar] = []
    redirects: list[Redirect] = list(extra_redirects)

    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]

        if ctype == "variable_assignment":
            ev = walk_variable_assignment(child, inner_commands, var_scope)
            if isinstance(ev, dict) and "kind" in ev:
                return ev
            # Env-prefix assignments are command-local — do NOT add to global var_scope.
            env_vars.append(EnvVar(name=ev["name"], value=ev["value"]))
        elif ctype == "command_name":
            first = child["children"][0] if child["children"] else child
            arg = walk_argument(first, inner_commands, var_scope)
            if not isinstance(arg, str):
                return arg
            argv.append(arg)
        elif ctype in (
            "word",
            "number",
            "raw_string",
            "string",
            "concatenation",
            "arithmetic_expansion",
        ):
            arg = walk_argument(child, inner_commands, var_scope)
            if not isinstance(arg, str):
                return arg
            argv.append(arg)
        elif ctype == "simple_expansion":
            # Bare `$VAR` as an argument.
            v = resolve_simple_expansion(child, var_scope, False)
            if not isinstance(v, str):
                return v
            argv.append(v)
        elif ctype == "file_redirect":
            r = walk_file_redirect(child, inner_commands, var_scope)
            if isinstance(r, dict) and "kind" in r:
                return r
            redirects.append(r)
        elif ctype == "herestring_redirect":
            err = walk_herestring_redirect(child, inner_commands, var_scope)
            if err:
                return err
        else:
            return too_complex(child)

    # Rebuild .text from argv when a $VAR was resolved (node.text diverges from argv) or when
    # node.text contains a newline (line-continuation leak).
    if _DOLLAR_IDENT_RE.search(node["text"]) or "\n" in node["text"]:
        parts = []
        for a in argv:
            if a == "" or _ARG_NEEDS_QUOTE_RE.search(a):
                parts.append("'" + a.replace("'", "'\\''") + "'")
            else:
                parts.append(a)
        text = " ".join(parts)
    else:
        text = node["text"]
    return {
        "kind": "simple",
        "commands": [
            SimpleCommand(argv=argv, envVars=env_vars, redirects=redirects, text=text)
        ],
    }


def collect_command_substitution(
    cs_node: Node,
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> ParseForSecurityResult | None:
    """Recurse into ``$()`` inner command(s)."""
    inner_scope = dict(var_scope)
    for child in cs_node["children"]:
        if not child:
            continue
        if child["type"] in ("$(", "`", ")"):
            continue
        err = collect_commands(child, inner_commands, inner_scope)
        if err:
            return err
    return None


def walk_argument(
    node: Node | None,
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> str | ParseForSecurityResult:
    """Argument-position allowlist; returns the literal string."""
    if not node:
        return {"kind": "too-complex", "reason": "Null argument node"}

    ntype = node["type"]

    if ntype == "word":
        if BRACE_EXPANSION_RE.search(node["text"]):
            return {
                "kind": "too-complex",
                "reason": "Word contains brace expansion syntax",
                "nodeType": "word",
            }
        return re.sub(r"\\(.)", r"\1", node["text"])

    if ntype == "number":
        # `NN#<expansion>` arithmetic base syntax: number node with expansion child.
        if len(node["children"]) > 0:
            return {
                "kind": "too-complex",
                "reason": "Number node contains expansion (NN# arithmetic base syntax)",
                "nodeType": node["children"][0]["type"] if node["children"] else None,
            }
        return node["text"]

    if ntype == "raw_string":
        return strip_literal_text(node["text"])

    if ntype == "string":
        return walk_string(node, inner_commands, var_scope)

    if ntype == "concatenation":
        if BRACE_EXPANSION_RE.search(node["text"]):
            return {
                "kind": "too-complex",
                "reason": "Brace expansion",
                "nodeType": "concatenation",
            }
        result = ""
        for child in node["children"]:
            if not child:
                continue
            part = walk_argument(child, inner_commands, var_scope)
            if not isinstance(part, str):
                return part
            result += part
        return result

    if ntype == "arithmetic_expansion":
        err = walk_arithmetic(node)
        if err:
            return err
        return node["text"]

    if ntype == "simple_expansion":
        # `$VAR` inside a concatenation counts as bare arg (insideString=False).
        return resolve_simple_expansion(node, var_scope, False)

    return too_complex(node)


def walk_string(
    node: Node,
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
) -> str | ParseForSecurityResult:
    """Extract literal content from a double-quoted ``string`` node.

    Tracks child ``startIndex``/``endIndex`` (UTF-8 byte offsets) and inserts one ``\\n`` per
    index gap (the tree-sitter dropped-newline quirk), exactly as the TS does.
    """
    result = ""
    cursor = -1
    saw_dynamic_placeholder = False
    saw_literal_content = False
    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]
        # Index gap between this child and the previous = dropped newline(s). Skip before the
        # first non-delimiter child (cursor == -1) and before closing `"` delimiters.
        if cursor != -1 and child["startIndex"] > cursor and ctype != '"':
            result += "\n" * (child["startIndex"] - cursor)
            saw_literal_content = True
        cursor = child["endIndex"]
        if ctype == '"':
            # Reset cursor after opening quote.
            cursor = child["endIndex"]
        elif ctype == "string_content":
            # Bash double-quote escape rules: `\` only escapes $ ` " \ inside "...".
            result += re.sub(r"\\([$`\"\\])", r"\1", child["text"])
            saw_literal_content = True
        elif ctype == DOLLAR:
            result += DOLLAR
            saw_literal_content = True
        elif ctype == "command_substitution":
            heredoc_body = extract_safe_cat_heredoc(child)
            if heredoc_body == "DANGEROUS":
                return too_complex(child)
            if heredoc_body is not None:
                trimmed = re.sub(r"\n+$", "", heredoc_body)
                if "\n" in trimmed:
                    saw_literal_content = True
                    continue
                result += trimmed
                saw_literal_content = True
                continue
            err = collect_command_substitution(child, inner_commands, var_scope)
            if err:
                return err
            result += CMDSUB_PLACEHOLDER
            saw_dynamic_placeholder = True
        elif ctype == "simple_expansion":
            v = resolve_simple_expansion(child, var_scope, True)
            if not isinstance(v, str):
                return v
            if v == VAR_PLACEHOLDER:
                saw_dynamic_placeholder = True
            else:
                saw_literal_content = True
            result += v
        elif ctype == "arithmetic_expansion":
            err = walk_arithmetic(child)
            if err:
                return err
            result += child["text"]
            saw_literal_content = True
        else:
            # expansion (${...}) inside "..."
            return too_complex(child)

    # Reject solo-placeholder strings.
    if saw_dynamic_placeholder and not saw_literal_content:
        return too_complex(node)
    # tree-sitter quirk: whitespace-only double-quoted string has no string_content child.
    if (
        not saw_literal_content
        and not saw_dynamic_placeholder
        and len(node["text"]) > 2
    ):
        return too_complex(node)
    return result


# Safe leaf nodes inside arithmetic expansion.
ARITH_LEAF_RE = re.compile(
    r"^(?:[0-9]+|0[xX][0-9a-fA-F]+|[0-9]+#[0-9a-zA-Z]+|"
    r"[-+*/%^&|~!<>=?:(),]+|<<|>>|\*\*|&&|\|\||[<>=!]=|\$\(\(|\)\))$"
)


def walk_arithmetic(node: Node) -> ParseForSecurityResult | None:
    """Allow only literal numeric expressions."""
    for child in node["children"]:
        if not child:
            continue
        if len(child["children"]) == 0:
            if not ARITH_LEAF_RE.match(child["text"]):
                return {
                    "kind": "too-complex",
                    "reason": (
                        "Arithmetic expansion references variable or non-literal: "
                        f"{child['text']}"
                    ),
                    "nodeType": "arithmetic_expansion",
                }
            continue
        if child["type"] in (
            "binary_expression",
            "unary_expression",
            "ternary_expression",
            "parenthesized_expression",
        ):
            err = walk_arithmetic(child)
            if err:
                return err
        else:
            return too_complex(child)
    return None


def extract_safe_cat_heredoc(sub_node: Node):
    """Extract the safe cat heredoc.

    Returns the heredoc body string for ``$(cat <<'DELIM'...DELIM)``, the string
    ``'DANGEROUS'`` for a body that reads ``/proc/*/environ`` or contains ``system(``, or
    ``None`` for any deviation.
    """
    stmt: Node | None = None
    for child in sub_node["children"]:
        if not child:
            continue
        if child["type"] in ("$(", ")"):
            continue
        if child["type"] == "redirected_statement" and stmt is None:
            stmt = child
        else:
            return None
    if not stmt:
        return None

    saw_cat = False
    body: str | None = None
    for child in stmt["children"]:
        if not child:
            continue
        ctype = child["type"]
        if ctype == "command":
            cmd_children = [c for c in child["children"] if c]
            if len(cmd_children) != 1:
                return None
            name_node = cmd_children[0]
            if name_node["type"] != "command_name" or name_node["text"] != "cat":
                return None
            saw_cat = True
        elif ctype == "heredoc_redirect":
            if walk_heredoc_redirect(child) is not None:
                return None
            for hc in child["children"]:
                if hc and hc["type"] == "heredoc_body":
                    body = hc["text"]
        else:
            return None

    if not saw_cat or body is None:
        return None
    if PROC_ENVIRON_RE.search(body):
        return "DANGEROUS"
    if re.search(r"\bsystem\s*\(", body):
        return "DANGEROUS"
    return body


def walk_variable_assignment(
    node: Node,
    inner_commands: list[SimpleCommand],
    var_scope: dict[str, str],
):
    """Collect a variable assignment and any nested substitutions.

    Returns ``{'name', 'value', 'isAppend'}`` on success or a too-complex result dict.
    """
    name: str | None = None
    value = ""
    is_append = False

    for child in node["children"]:
        if not child:
            continue
        ctype = child["type"]
        if ctype == "variable_name":
            name = child["text"]
        elif ctype in ("=", "+="):
            is_append = ctype == "+="
            continue
        elif ctype == "command_substitution":
            err = collect_command_substitution(child, inner_commands, var_scope)
            if err:
                return err
            value = CMDSUB_PLACEHOLDER
        elif ctype == "simple_expansion":
            # Assignment RHS does NOT word-split/glob — resolve as insideString=True.
            v = resolve_simple_expansion(child, var_scope, True)
            if not isinstance(v, str):
                return v
            value = v
        else:
            v = walk_argument(child, inner_commands, var_scope)
            if not isinstance(v, str):
                return v
            value = v

    if name is None:
        return {
            "kind": "too-complex",
            "reason": "Variable assignment without name",
            "nodeType": "variable_assignment",
        }
    # tree-sitter accepts invalid var names; bash runs them as a COMMAND.
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return {
            "kind": "too-complex",
            "reason": f"Invalid variable name (bash treats as command): {name}",
            "nodeType": "variable_assignment",
        }
    if name == "IFS":
        return {
            "kind": "too-complex",
            "reason": "IFS assignment changes word-splitting — cannot model statically",
            "nodeType": "variable_assignment",
        }
    if name == "PS4":
        if is_append:
            return {
                "kind": "too-complex",
                "reason": (
                    "PS4 += cannot be statically verified — combine into a single "
                    "PS4= assignment"
                ),
                "nodeType": "variable_assignment",
            }
        if contains_any_placeholder(value):
            return {
                "kind": "too-complex",
                "reason": "PS4 value derived from cmdsub/variable — runtime unknowable",
                "nodeType": "variable_assignment",
            }
        stripped = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", value)
        if not re.match(r"^[A-Za-z0-9 _+:./=\[\]-]*$", stripped):
            return {
                "kind": "too-complex",
                "reason": (
                    "PS4 value outside safe charset — only ${VAR} refs and "
                    "[A-Za-z0-9 _+:.=/[]-] allowed"
                ),
                "nodeType": "variable_assignment",
            }
    if "~" in value:
        return {
            "kind": "too-complex",
            "reason": "Tilde in assignment value — bash may expand at assignment time",
            "nodeType": "variable_assignment",
        }
    return {"name": name, "value": value, "isAppend": is_append}


def resolve_simple_expansion(
    node: Node,
    var_scope: dict[str, str],
    inside_string: bool,
) -> str | ParseForSecurityResult:
    """Resolve a ``simple_expansion`` (``$VAR``)."""
    var_name: str | None = None
    is_special = False
    for c in node["children"]:
        if c and c["type"] == "variable_name":
            var_name = c["text"]
            break
        if c and c["type"] == "special_variable_name":
            var_name = c["text"]
            is_special = True
            break
    if var_name is None:
        return too_complex(node)
    tracked_value = var_scope.get(var_name)
    if tracked_value is not None:
        if contains_any_placeholder(tracked_value):
            if not inside_string:
                return too_complex(node)
            return VAR_PLACEHOLDER
        if not inside_string:
            if tracked_value == "":
                return too_complex(node)
            if BARE_VAR_UNSAFE_RE.search(tracked_value):
                return too_complex(node)
        return tracked_value
    if inside_string:
        if var_name in SAFE_ENV_VARS:
            return VAR_PLACEHOLDER
        if is_special and (
            var_name in SPECIAL_VAR_NAMES or re.match(r"^[0-9]+$", var_name)
        ):
            return VAR_PLACEHOLDER
    return too_complex(node)


def apply_var_to_scope(var_scope: dict[str, str], ev: dict) -> None:
    """Apply an assignment, handling ``+=`` append."""
    existing = var_scope.get(ev["name"], "")
    combined = existing + ev["value"] if ev["isAppend"] else ev["value"]
    var_scope[ev["name"]] = (
        VAR_PLACEHOLDER if contains_any_placeholder(combined) else combined
    )


def strip_literal_text(text: str) -> str:
    """Strip the first/last char (raw-string quotes)."""
    return text[1:-1]


def too_complex(node: Node) -> ParseForSecurityResult:
    """Build a too-complex result for an unhandled node."""
    ntype = node["type"]
    if ntype == "ERROR":
        reason = "Parse error"
    elif ntype in _DANGEROUS_TYPES_SET:
        reason = f"Contains {ntype}"
    else:
        reason = f"Unhandled node type: {ntype}"
    return {"kind": "too-complex", "reason": reason, "nodeType": ntype}


# ── Post-argv semantic checks ────────────────────────────────────────────────

# Zsh module builtins (loaded via zmodload).
ZSH_DANGEROUS_BUILTINS = {
    "zmodload",
    "emulate",
    "sysopen",
    "sysread",
    "syswrite",
    "sysseek",
    "zpty",
    "ztcp",
    "zsocket",
    "zf_rm",
    "zf_mv",
    "zf_ln",
    "zf_chmod",
    "zf_chown",
    "zf_mkdir",
    "zf_rmdir",
    "zf_chgrp",
}

# Shell builtins that evaluate their arguments as code.
EVAL_LIKE_BUILTINS = {
    "eval",
    "source",
    ".",
    "exec",
    "command",
    "builtin",
    "fc",
    "coproc",
    "noglob",
    "nocorrect",
    "trap",
    "enable",
    "mapfile",
    "readarray",
    "hash",
    "bind",
    "complete",
    "compgen",
    "alias",
    "let",
}

# Builtins that re-parse a NAME operand and arithmetically evaluate arr[EXPR] subscripts.
SUBSCRIPT_EVAL_FLAGS = {
    "test": {"-v", "-R"},
    "[": {"-v", "-R"},
    "[[": {"-v", "-R"},
    "printf": {"-v"},
    "read": {"-a"},
    "unset": {"-v"},
    "wait": {"-p"},
}

# `[[ ARG1 OP ARG2 ]]` arithmetic comparison operators.
TEST_ARITH_CMP_OPS = {"-eq", "-ne", "-lt", "-le", "-gt", "-ge"}

# Builtins where EVERY non-flag positional is a NAME (subscript-evaluating).
BARE_SUBSCRIPT_NAME_BUILTINS = {"read", "unset"}

# `read` flags whose NEXT argument is data, not a NAME.
READ_DATA_FLAGS = {"-p", "-d", "-n", "-N", "-t", "-u", "-i"}

# /proc/*/environ — use `.*` not `[^/]*` (Linux resolves `..` in procfs).
PROC_ENVIRON_RE = re.compile(r"/proc/.*/environ")

# Newline followed by `#` in an argv element / env var value / redirect target.
NEWLINE_HASH_RE = re.compile(r"\n[ \t]*#")


def _strip_safe_wrappers(argv: list[str]) -> SemanticCheckResult | list[str]:
    """Inlined wrapper-stripping (nohup/time/timeout/nice/env/stdbuf) from ``checkSemantics``.

    Returns the stripped argv list, or a ``{'ok': False, 'reason'}`` dict on fail-closed.
    """
    a = argv
    while True:
        if a[0:1] == ["time"] or a[0:1] == ["nohup"]:
            a = a[1:]
        elif a[0:1] == ["timeout"]:
            i = 1
            while i < len(a):
                arg = a[i]
                if arg in ("--foreground", "--preserve-status", "--verbose"):
                    i += 1
                elif re.match(r"^--(?:kill-after|signal)=[A-Za-z0-9_.+-]+$", arg):
                    i += 1
                elif (
                    arg in ("--kill-after", "--signal")
                    and i + 1 < len(a)
                    and a[i + 1]
                    and re.match(r"^[A-Za-z0-9_.+-]+$", a[i + 1])
                ):
                    i += 2
                elif arg.startswith("--"):
                    return {
                        "ok": False,
                        "reason": f"timeout with {arg} flag cannot be statically analyzed",
                    }
                elif arg == "-v":
                    i += 1
                elif (
                    arg in ("-k", "-s")
                    and i + 1 < len(a)
                    and a[i + 1]
                    and re.match(r"^[A-Za-z0-9_.+-]+$", a[i + 1])
                ):
                    i += 2
                elif re.match(r"^-[ks][A-Za-z0-9_.+-]+$", arg):
                    i += 1
                elif arg.startswith("-"):
                    return {
                        "ok": False,
                        "reason": f"timeout with {arg} flag cannot be statically analyzed",
                    }
                else:
                    break
            if i < len(a) and re.match(r"^\d+(?:\.\d+)?[smhd]?$", a[i]):
                a = a[i + 1 :]
            elif i < len(a):
                return {
                    "ok": False,
                    "reason": f"timeout duration '{a[i]}' cannot be statically analyzed",
                }
            else:
                break
        elif a[0:1] == ["nice"]:
            if len(a) > 1 and a[1] == "-n" and len(a) > 2 and re.match(r"^-?\d+$", a[2]):
                a = a[3:]
            elif len(a) > 1 and a[1] and re.match(r"^-\d+$", a[1]):
                a = a[2:]
            elif len(a) > 1 and a[1] and re.search(r"[$(`]", a[1]):
                return {
                    "ok": False,
                    "reason": (
                        f"nice argument '{a[1]}' contains expansion — cannot statically "
                        "determine wrapped command"
                    ),
                }
            else:
                a = a[1:]
        elif a[0:1] == ["env"]:
            i = 1
            while i < len(a):
                arg = a[i]
                if "=" in arg and not arg.startswith("-"):
                    i += 1
                elif arg in ("-i", "-0", "-v"):
                    i += 1
                elif arg == "-u" and i + 1 < len(a) and a[i + 1]:
                    i += 2
                elif arg.startswith("-"):
                    return {
                        "ok": False,
                        "reason": f"env with {arg} flag cannot be statically analyzed",
                    }
                else:
                    break
            if i < len(a):
                a = a[i:]
            else:
                break
        elif a[0:1] == ["stdbuf"]:
            i = 1
            while i < len(a):
                arg = a[i]
                if STDBUF_SHORT_SEP_RE.match(arg) and i + 1 < len(a) and a[i + 1]:
                    i += 2
                elif STDBUF_SHORT_FUSED_RE.match(arg):
                    i += 1
                elif STDBUF_LONG_RE.match(arg):
                    i += 1
                elif arg.startswith("-"):
                    return {
                        "ok": False,
                        "reason": f"stdbuf with {arg} flag cannot be statically analyzed",
                    }
                else:
                    break
            if i > 1 and i < len(a):
                a = a[i:]
            else:
                break
        else:
            break
    return a


def check_semantics(commands: list[SimpleCommand]) -> SemanticCheckResult:
    """Post-argv semantic checks (name/argument content)."""
    for cmd in commands:
        stripped = _strip_safe_wrappers(cmd.argv)
        if isinstance(stripped, dict):
            return stripped
        a = stripped

        name = a[0] if a else None
        if name is None:
            continue

        if name == "":
            return {
                "ok": False,
                "reason": "Empty command name — argv[0] may not reflect what bash runs",
            }

        if CMDSUB_PLACEHOLDER in name or VAR_PLACEHOLDER in name:
            return {
                "ok": False,
                "reason": "Command name is runtime-determined (placeholder argv[0])",
            }

        if name.startswith("-") or name.startswith("|") or name.startswith("&"):
            return {
                "ok": False,
                "reason": "Command appears to be an incomplete fragment",
            }

        danger_flags = SUBSCRIPT_EVAL_FLAGS.get(name)
        if danger_flags is not None:
            for i in range(1, len(a)):
                arg = a[i]
                nxt = a[i + 1] if i + 1 < len(a) else None
                # Separate form: `-v` then NAME in next arg.
                if arg in danger_flags and nxt is not None and "[" in nxt:
                    return {
                        "ok": False,
                        "reason": (
                            f"'{name} {arg}' operand contains array subscript — "
                            "bash evaluates $(cmd) in subscripts"
                        ),
                    }
                # Combined short flags: `-ra` is bash shorthand for `-r -a`.
                if (
                    len(arg) > 2
                    and arg[0] == "-"
                    and arg[1] != "-"
                    and "[" not in arg
                ):
                    for flag in danger_flags:
                        if len(flag) == 2 and flag[1] in arg:
                            if nxt is not None and "[" in nxt:
                                return {
                                    "ok": False,
                                    "reason": (
                                        f"'{name} {flag}' (combined in '{arg}') operand "
                                        "contains array subscript — bash evaluates $(cmd) "
                                        "in subscripts"
                                    ),
                                }
                # Fused form: `-vNAME` in one arg.
                for flag in danger_flags:
                    if (
                        len(flag) == 2
                        and arg.startswith(flag)
                        and len(arg) > 2
                        and "[" in arg
                    ):
                        return {
                            "ok": False,
                            "reason": (
                                f"'{name} {flag}' (fused) operand contains array subscript "
                                "— bash evaluates $(cmd) in subscripts"
                            ),
                        }

        if name == "[[":
            for i in range(2, len(a)):
                if a[i] not in TEST_ARITH_CMP_OPS:
                    continue
                prev = a[i - 1] if i - 1 >= 0 else None
                nxt = a[i + 1] if i + 1 < len(a) else None
                if (prev is not None and "[" in prev) or (nxt is not None and "[" in nxt):
                    return {
                        "ok": False,
                        "reason": (
                            f"'[[ ... {a[i]} ... ]]' operand contains array subscript — "
                            "bash arithmetically evaluates $(cmd) in subscripts"
                        ),
                    }

        if name in BARE_SUBSCRIPT_NAME_BUILTINS:
            skip_next = False
            for i in range(1, len(a)):
                arg = a[i]
                if skip_next:
                    skip_next = False
                    continue
                if arg[0:1] == "-":
                    if name == "read":
                        if arg in READ_DATA_FLAGS:
                            skip_next = True
                        elif len(arg) > 2 and arg[1] != "-":
                            for j in range(1, len(arg)):
                                if ("-" + arg[j]) in READ_DATA_FLAGS:
                                    if j == len(arg) - 1:
                                        skip_next = True
                                    break
                    continue
                if "[" in arg:
                    return {
                        "ok": False,
                        "reason": (
                            f"'{name}' positional NAME '{arg}' contains array subscript — "
                            "bash evaluates $(cmd) in subscripts"
                        ),
                    }

        if name in SHELL_KEYWORDS:
            return {
                "ok": False,
                "reason": (
                    f"Shell keyword '{name}' as command name — tree-sitter mis-parse"
                ),
            }

        for arg in cmd.argv:
            if "\n" in arg and NEWLINE_HASH_RE.search(arg):
                return {
                    "ok": False,
                    "reason": (
                        "Newline followed by # inside a quoted argument can hide arguments "
                        "from path validation"
                    ),
                }
        for ev in cmd.envVars:
            if "\n" in ev.value and NEWLINE_HASH_RE.search(ev.value):
                return {
                    "ok": False,
                    "reason": (
                        "Newline followed by # inside an env var value can hide arguments "
                        "from path validation"
                    ),
                }
        for r in cmd.redirects:
            if "\n" in r.target and NEWLINE_HASH_RE.search(r.target):
                return {
                    "ok": False,
                    "reason": (
                        "Newline followed by # inside a redirect target can hide arguments "
                        "from path validation"
                    ),
                }

        if name == "jq":
            for arg in a:
                if re.search(r"\bsystem\s*\(", arg):
                    return {
                        "ok": False,
                        "reason": (
                            "jq command contains system() function which executes "
                            "arbitrary commands"
                        ),
                    }
            if any(
                re.match(
                    r"^(?:-[fL](?:$|[^A-Za-z])|"
                    r"--(?:from-file|rawfile|slurpfile|library-path)(?:$|=))",
                    arg,
                )
                for arg in a
            ):
                return {
                    "ok": False,
                    "reason": (
                        "jq command contains dangerous flags that could execute code or "
                        "read arbitrary files"
                    ),
                }

        if name in ZSH_DANGEROUS_BUILTINS:
            return {
                "ok": False,
                "reason": f"Zsh builtin '{name}' can bypass security checks",
            }

        if name in EVAL_LIKE_BUILTINS:
            if name == "command" and (a[1:2] == ["-v"] or a[1:2] == ["-V"]):
                pass  # `command -v/-V foo` — POSIX existence check, safe.
            elif name == "fc" and not any(
                re.match(r"^-[^-]*[es]", arg) for arg in a[1:]
            ):
                pass  # `fc -l`/`fc -ln` list history — safe.
            elif name == "compgen" and not any(
                re.match(r"^-[^-]*[CFW]", arg) for arg in a[1:]
            ):
                pass  # `compgen -c/-f/-v` list completions only — safe.
            else:
                return {
                    "ok": False,
                    "reason": f"'{name}' evaluates arguments as shell code",
                }

        for arg in cmd.argv:
            if "/proc/" in arg and PROC_ENVIRON_RE.search(arg):
                return {
                    "ok": False,
                    "reason": "Accesses /proc/*/environ which may expose secrets",
                }
        for r in cmd.redirects:
            if "/proc/" in r.target and PROC_ENVIRON_RE.search(r.target):
                return {
                    "ok": False,
                    "reason": "Accesses /proc/*/environ which may expose secrets",
                }
    return {"ok": True}


__all__ = [
    "CMDSUB_PLACEHOLDER",
    "DANGEROUS_TYPES",
    "EVAL_LIKE_BUILTINS",
    "PROC_ENVIRON_RE",
    "SAFE_ENV_VARS",
    "SHELL_KEYWORDS",
    "SPECIAL_VAR_NAMES",
    "VAR_PLACEHOLDER",
    "ZSH_DANGEROUS_BUILTINS",
    "EnvVar",
    "ParseForSecurityResult",
    "Redirect",
    "SemanticCheckResult",
    "SimpleCommand",
    "check_semantics",
    "node_type_id",
    "parse_for_security",
    "parse_for_security_from_ast",
    "walk_program",
]
