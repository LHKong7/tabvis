"""Safe wrappers for shell-quote library functions that handle errors gracefully.

The npm ``shell-quote`` package (``parse.js`` /
``quote.js``, v1.8.3) is **hand-implemented** below into :func:`shell_quote_parse` /
:func:`shell_quote_quote` (the tokenizer + the quote serializer), since there is no PyPI
equivalent we want to depend on. The drop-in safe wrappers (``try_parse_shell_command``,
``has_malformed_tokens``, ``has_shell_quote_single_quote_bug``, ``quote``) faithfully mirror
the TS behaviour the Bash-tool security layer relies on.

A ``ParseEntry`` is one of:

- ``str`` — a literal token (a parsed argument)
- ``{'op': str}`` — a control operator (``|``, ``&&``, ``;``, ``<``, ``>``, …)
- ``{'op': 'glob', 'pattern': str}`` — a glob token (contains ``*`` or ``?`` unquoted)
- ``{'comment': str}`` — a trailing comment (text after an unquoted ``#``)

These dict-shaped nodes keep the npm wire field names (``op`` / ``pattern`` / ``comment``)
verbatim.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from ..log import log_error
from ..slow_operations import json_stringify

__all__ = [
    "ParseEntry",
    "ShellParseResult",
    "ShellQuoteResult",
    "shell_quote_parse",
    "shell_quote_quote",
    "try_parse_shell_command",
    "try_quote_shell_args",
    "has_malformed_tokens",
    "has_shell_quote_single_quote_bug",
    "quote",
]

# A parsed entry is a literal string token, or one of the op/glob/comment dict nodes.
ParseEntry = str | dict

# ---------------------------------------------------------------------------
# Shell-word parser with quote, escape, comment, and environment expansion support.
# ---------------------------------------------------------------------------

# '<(' is process substitution operator and can be parsed the same as control operator.
_CONTROL = "(?:" + "|".join(
    [
        r"\|\|",
        r"\&\&",
        ";;",
        r"\|\&",
        r"\<\(",
        r"\<\<\<",
        ">>",
        r">\&",
        r"<\&",
        r"[&;()|<>]",
    ]
) + ")"
_control_re = re.compile("^" + _CONTROL + "$")
_META = "|&;()<> \\t"
# In JS these are the SINGLE_QUOTE / DOUBLE_QUOTE source fragments. (The TS/JS names are
# swapped vs. their actual quote char, but the *patterns* are what matter; preserved verbatim.)
_SINGLE_QUOTE = r'"((\\"|[^"])*?)"'
_DOUBLE_QUOTE = r"'((\\'|[^'])*?)'"
_hash = re.compile(r"^#$")

_SQ = "'"
_DQ = '"'
_DS = "$"

# A randomized token used to splice object env values back in (env-as-function path).
# npm builds it from Math.random hex; any opaque marker works — we only need uniqueness.
_TOKEN = "5f3759df5f3759df5f3759df5f3759df"
_starts_with_token = re.compile("^" + re.escape(_TOKEN))

# Inside parseEnvVar: bash special single-char variable names.
_SPECIAL_VAR_RE = re.compile(r"[*@#?$!_-]")
_NON_WORD_RE = re.compile(r"[^\w\d_]")


def _get_var(
    env: Any,
    pre: str,
    key: str,
) -> str:
    if callable(env):
        r = env(key)
    elif isinstance(env, dict):
        r = env.get(key)
    else:
        r = None
    if r is None and key != "":
        r = ""
    elif r is None:
        r = "$"

    # JS `typeof r === 'object'` — splice the JSON-encoded object between TOKEN markers.
    if isinstance(r, (dict, list)):
        return pre + _TOKEN + json.dumps(r) + _TOKEN
    return pre + str(r)


def _match_all(s: str, regex: re.Pattern[str]) -> list[re.Match[str]]:
    """Mirror npm's ``matchAll``: iterate ``regex.exec`` over ``s`` advancing on empty matches."""
    matches: list[re.Match[str]] = []
    pos = 0
    n = len(s)
    while pos <= n:
        m = regex.search(s, pos)
        if m is None:
            break
        matches.append(m)
        if m.end() == m.start():
            pos = m.end() + 1
        else:
            pos = m.end()
    return matches


def _parse_env_var(s: str, state: dict, env: Any) -> str:
    """Reads a ``$VAR`` / ``${VAR}`` at ``state['i']``."""
    state["i"] += 1
    i = state["i"]
    char = s[i] if i < len(s) else ""

    if char == "{":
        state["i"] += 1
        i = state["i"]
        if (s[i] if i < len(s) else "") == "}":
            raise ValueError("Bad substitution: " + s[i - 2 : i + 1])
        varend = s.find("}", i)
        if varend < 0:
            raise ValueError("Bad substitution: " + s[i:])
        varname = s[i:varend]
        state["i"] = varend
    elif _SPECIAL_VAR_RE.match(char):
        varname = char
        state["i"] += 1
    else:
        sliced_from_i = s[i:]
        m = _NON_WORD_RE.search(sliced_from_i)
        if not m:
            varname = sliced_from_i
            state["i"] = len(s)
        else:
            varname = sliced_from_i[: m.start()]
            state["i"] += m.start() - 1
    return _get_var(env, "", varname)


def _parse_chunk(
    s: str,
    string: str,
    match_start: int,
    env: Any,
    bs: str,
) -> tuple[list[ParseEntry], bool]:
    """Parse one chunker match into entries.

    Returns ``(entries, commented)`` — ``commented`` is True if an unquoted ``#`` started a
    comment (the caller then drops all subsequent chunks).
    """
    # Hand-written scanner for Bash quoting rules (see npm parse.js commentary).
    state: dict = {"quote": False, "esc": False, "out": "", "is_glob": False, "i": 0}

    i = 0
    length = len(s)
    while i < length:
        state["i"] = i
        c = s[i]
        state["is_glob"] = state["is_glob"] or (
            not state["quote"] and (c == "*" or c == "?")
        )
        if state["esc"]:
            state["out"] += c
            state["esc"] = False
        elif state["quote"]:
            if c == state["quote"]:
                state["quote"] = False
            elif state["quote"] == _SQ:
                state["out"] += c
            else:  # Double quote
                if c == bs:
                    i += 1
                    state["i"] = i
                    c = s[i] if i < len(s) else ""
                    if c == _DQ or c == bs or c == _DS:
                        state["out"] += c
                    else:
                        state["out"] += bs + c
                elif c == _DS:
                    state["out"] += _parse_env_var(s, state, env)
                    i = state["i"]
                else:
                    state["out"] += c
        elif c == _DQ or c == _SQ:
            state["quote"] = c
        elif _control_re.match(c):
            return [{"op": s}], False
        elif _hash.match(c):
            comment_obj: dict = {"comment": string[match_start + i + 1 :]}
            if len(state["out"]):
                return [state["out"], comment_obj], True
            return [comment_obj], True
        elif c == bs:
            state["esc"] = True
        elif c == _DS:
            state["out"] += _parse_env_var(s, state, env)
            i = state["i"]
        else:
            state["out"] += c
        i += 1

    if state["is_glob"]:
        return [{"op": "glob", "pattern": state["out"]}], False
    return [state["out"]], False


def _parse_internal(
    string: str,
    env: Any,
    opts: dict | None = None,
) -> list[ParseEntry]:
    if not opts:
        opts = {}
    bs = opts.get("escape") or "\\"
    # BAREWORD = (\\[escaped meta/quote] | [^whitespace, quotes, meta])+
    bareword = "(\\" + bs + "['\"" + _META + "]|[^\\s'\"" + _META + "])+"

    chunker = re.compile(
        "(" + _CONTROL + ")"  # control chars
        + "|"
        + "(" + bareword + "|" + _SINGLE_QUOTE + "|" + _DOUBLE_QUOTE + ")+"
    )

    matches = _match_all(string, chunker)
    if len(matches) == 0:
        return []
    if not env:
        env = {}

    commented = False
    out_entries: list[ParseEntry] = []

    for match in matches:
        s = match.group(0)
        if not s or commented:
            continue
        if _control_re.match(s):
            out_entries.append({"op": s})
            continue

        entries, became_comment = _parse_chunk(s, string, match.start(), env, bs)
        out_entries.extend(entries)
        if became_comment:
            commented = True

    return out_entries


def shell_quote_parse(
    s: str,
    env: Any = None,
    opts: dict | None = None,
) -> list[ParseEntry]:
    """Parse a shell command into literal and operator tokens.

    Tokenizes ``s`` into a list of :data:`ParseEntry`. ``env`` may be a dict or a callable
    ``(key) -> value | None`` for variable expansion (the function path additionally splices
    object env values back via the TOKEN marker, matching npm).
    """
    mapped = _parse_internal(s, env, opts)
    if not callable(env):
        return mapped

    splitter = re.compile("(" + re.escape(_TOKEN) + ".*?" + re.escape(_TOKEN) + ")")
    acc: list[ParseEntry] = []
    for item in mapped:
        if isinstance(item, dict):
            acc.append(item)
            continue
        xs = splitter.split(item)
        if len(xs) == 1:
            acc.append(xs[0])
            continue
        for x in xs:
            if not x:
                continue
            if _starts_with_token.match(x):
                acc.append(json.loads(x.split(_TOKEN)[1]))
            else:
                acc.append(x)
    return acc


# ---------------------------------------------------------------------------
# Shell-safe quoting for strings and operator tokens.
# ---------------------------------------------------------------------------

_QUOTE_NEEDS_BS = re.compile(r'["\s\\]')
_QUOTE_HAS_SQ = re.compile(r"'")
_QUOTE_HAS_QUOTE_OR_WS = re.compile(r"[\"'\s]")
_DQ_ESCAPE = re.compile(r'(["\\$`!])')
_SQ_ESCAPE = re.compile(r"(['])")
# Final fallback escaper: optional drive prefix then a single shell metacharacter.
_FALLBACK_ESCAPE = re.compile(r"([A-Za-z]:)?([#!\"$&'()*,:;<=>?@\[\\\]^`{|}])")


def shell_quote_quote(xs: list[Any]) -> str:
    """Quote shell arguments and operators for safe reconstruction.

    Serializes a list of string tokens (or ``{'op': ...}`` nodes) back into a shell-safe
    command string.
    """
    parts: list[str] = []
    for s in xs:
        if s == "":
            parts.append("''")
            continue
        if s and isinstance(s, dict):
            parts.append(re.sub(r"(.)", r"\\\1", s["op"]))
            continue
        s = str(s)
        if _QUOTE_NEEDS_BS.search(s) and not _QUOTE_HAS_SQ.search(s):
            parts.append("'" + _SQ_ESCAPE.sub(r"\\\1", s) + "'")
            continue
        if _QUOTE_HAS_QUOTE_OR_WS.search(s):
            parts.append('"' + _DQ_ESCAPE.sub(r"\\\1", s) + '"')
            continue
        parts.append(_FALLBACK_ESCAPE.sub(r"\1\\\2", s))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Safe wrappers
# ---------------------------------------------------------------------------

ShellParseResult = dict  # {'success': True, 'tokens': [...]} | {'success': False, 'error': str}
ShellQuoteResult = dict  # {'success': True, 'quoted': str} | {'success': False, 'error': str}


def try_parse_shell_command(
    cmd: str,
    env: dict | Callable[[str], Any] | None = None,
) -> ShellParseResult:
    try:
        tokens = shell_quote_parse(cmd, env)
        return {"success": True, "tokens": tokens}
    except Exception as error:  # noqa: BLE001 — faithful to the TS catch-all
        if isinstance(error, Exception):
            log_error(error)
        return {
            "success": False,
            "error": str(error) if str(error) else "Unknown parse error",
        }


def try_quote_shell_args(args: list[Any]) -> ShellQuoteResult:
    try:
        validated: list[str] = []
        for index, arg in enumerate(args):
            if arg is None:
                # JS String(null) === 'null', String(undefined) === 'undefined'.
                validated.append("null" if arg is None else "undefined")
                continue
            if isinstance(arg, str):
                validated.append(arg)
            elif isinstance(arg, bool):
                validated.append("true" if arg else "false")
            elif isinstance(arg, (int, float)):
                validated.append(_js_number_str(arg))
            elif callable(arg):
                raise ValueError(
                    f"Cannot quote argument at index {index}: function values are not supported"
                )
            elif isinstance(arg, (dict, list)):
                raise ValueError(
                    f"Cannot quote argument at index {index}: object values are not supported"
                )
            else:
                raise ValueError(
                    f"Cannot quote argument at index {index}: "
                    f"unsupported type {type(arg).__name__}"
                )

        quoted = shell_quote_quote(validated)
        return {"success": True, "quoted": quoted}
    except Exception as error:  # noqa: BLE001
        if isinstance(error, Exception):
            log_error(error)
        return {
            "success": False,
            "error": str(error) if str(error) else "Unknown quote error",
        }


def _js_number_str(n: int | float) -> str:
    """Render a number the way JS ``String(n)`` would (no trailing ``.0`` for integers)."""
    if isinstance(n, bool):  # defensive — bool is a subclass of int
        return "true" if n else "false"
    if isinstance(n, int):
        return str(n)
    if n == int(n):
        return str(int(n))
    return repr(n)


_BRACE_OPEN = re.compile(r"{")
_BRACE_CLOSE = re.compile(r"}")
_PAREN_OPEN = re.compile(r"\(")
_PAREN_CLOSE = re.compile(r"\)")
_BRACKET_OPEN = re.compile(r"\[")
_BRACKET_CLOSE = re.compile(r"\]")
_UNESCAPED_DQ = re.compile(r'(?<!\\)"')
_UNESCAPED_SQ = re.compile(r"(?<!\\)'")


def has_malformed_tokens(command: str, parsed: list[ParseEntry]) -> bool:
    """Detects shell-quote misparses (HackerOne #3482049).

    Walks the raw command with bash quote semantics to flag unterminated quotes, then checks
    each literal token for unbalanced braces/parens/brackets and odd unescaped quote parity.
    """
    in_single = False
    in_double = False
    double_count = 0
    single_count = 0
    i = 0
    length = len(command)
    while i < length:
        c = command[i]
        if c == "\\" and not in_single:
            i += 1
            i += 1
            continue
        if c == '"' and not in_single:
            double_count += 1
            in_double = not in_double
        elif c == "'" and not in_double:
            single_count += 1
            in_single = not in_single
        i += 1
    if double_count % 2 != 0 or single_count % 2 != 0:
        return True

    for entry in parsed:
        if not isinstance(entry, str):
            continue

        if len(_BRACE_OPEN.findall(entry)) != len(_BRACE_CLOSE.findall(entry)):
            return True
        if len(_PAREN_OPEN.findall(entry)) != len(_PAREN_CLOSE.findall(entry)):
            return True
        if len(_BRACKET_OPEN.findall(entry)) != len(_BRACKET_CLOSE.findall(entry)):
            return True
        if len(_UNESCAPED_DQ.findall(entry)) % 2 != 0:
            return True
        if len(_UNESCAPED_SQ.findall(entry)) % 2 != 0:
            return True
    return False


def has_shell_quote_single_quote_bug(command: str) -> bool:
    """Return whether shell quote single quote bug.

    Detects ``'\\'`` patterns that exploit shell-quote's incorrect handling of backslashes
    inside single quotes (bash treats backslash literally inside single quotes; shell-quote
    treats it as an escape, merging tokens that bash keeps separate).
    """
    in_single_quote = False
    in_double_quote = False

    i = 0
    length = len(command)
    while i < length:
        char = command[i]

        if char == "\\" and not in_single_quote:
            i += 1
            i += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            i += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote

            if not in_single_quote:
                backslash_count = 0
                j = i - 1
                while j >= 0 and command[j] == "\\":
                    backslash_count += 1
                    j -= 1
                if backslash_count > 0 and backslash_count % 2 == 1:
                    return True
                if (
                    backslash_count > 0
                    and backslash_count % 2 == 0
                    and command.find("'", i + 1) != -1
                ):
                    return True
            i += 1
            continue

        i += 1

    return False


def quote(args: list[Any]) -> str:
    """Strict shell quoting with a lenient (but still safe) fallback.

    Never falls back to JSON serialization for shell quoting (JSON double-quotes don't
    prevent command execution).
    """
    result = try_quote_shell_args(list(args))

    if result["success"]:
        return result["quoted"]

    try:
        string_args: list[str] = []
        for arg in args:
            if arg is None:
                string_args.append("null" if arg is None else "undefined")
                continue
            if isinstance(arg, bool):
                string_args.append("true" if arg else "false")
            elif isinstance(arg, str):
                string_args.append(arg)
            elif isinstance(arg, (int, float)):
                string_args.append(_js_number_str(arg))
            else:
                # For unsupported types, use json serialization as a safe *representation*
                # (not as a quoting mechanism — that output is still passed through quote()).
                string_args.append(json_stringify(arg))

        return shell_quote_quote(string_args)
    except Exception as error:  # noqa: BLE001
        if isinstance(error, Exception):
            log_error(error)
        raise ValueError("Failed to quote shell arguments safely") from error
