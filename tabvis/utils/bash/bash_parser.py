"""Pure-Python bash parser

Produces tree-sitter-bash-compatible ASTs. Downstream code in ``parser`` / ``ast`` /
``prefix`` / ``parsed_command`` walks this by field name. ``start_index``/``end_index`` are
**UTF-8 BYTE offsets** (not Python ``str`` indices) — tracked faithfully where the TS does.

RUNTIME-GATED-OFF: the parser feature gates are hardcoded to ``false``, so
``parse_command_raw`` / ``parse_command`` always return ``None`` at runtime and this whole
module is **dead at runtime**. It exists because its EXPORTS must exist and import
cleanly (the :data:`SHELL_KEYWORDS` set, the :class:`TsNode` node shape, and the parse entry
:func:`get_parser_module` / :func:`ensure_parser_initialized` / :func:`parse_source`) for
``ast`` + the BashTool consumers.

AST node shape (dict, wire field names kept verbatim)::

    {"type": str, "text": str, "startIndex": int (utf8 byte), "endIndex": int (utf8 byte),
     "children": list[TsNode]}

STATUS: the lexer/tokenizer, the parse-entry machinery, the budget/node builders, and
the top-of-grammar (``parse_program`` / ``parse_statements`` / ``parse_and_or``) are
implemented. The deep recursive-descent grammar (``parse_pipeline`` and below — commands,
redirects, words, expansions, test/arith expressions, heredoc bodies) is not implemented in
this build; those functions are raising stubs. Because the
gates are off, ``parse_source`` is never reached at runtime; if it ever were, the stubs raise
and :func:`parse_source` catches and returns ``None`` (fail-closed), preserving behavior.

Casing: Python identifiers snake_case; classes PascalCase; constants UPPER_CASE. The AST node
dicts keep their tree-sitter wire keys (``startIndex``/``endIndex``).
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

# ───────────────────────────── Node shape ─────────────────────────────


class TsNode(TypedDict):
    """tree-sitter-bash-compatible AST node. ``startIndex``/``endIndex`` are UTF-8 byte offsets.

    Field names are kept verbatim (wire keys) so downstream walkers read identically to the TS.
    """

    type: str
    text: str
    startIndex: int
    endIndex: int
    children: list[TsNode]


@dataclass
class ParserModule:
    """Mirror of the TS ``ParserModule`` ``{ parse }`` shape."""

    parse: Callable[..., TsNode | None]


# 50ms wall-clock cap — bails out on pathological/adversarial input.
# Pass ``timeout_ms=float("inf")`` to disable (e.g. correctness tests).
PARSE_TIMEOUT_MS = 50

# Node budget cap — bails out before OOM on deeply nested input.
MAX_NODES = 50_000


def ensure_parser_initialized() -> Awaitable[None]:
    """No-op: pure-Python parser needs no async init. Kept for API compatibility.

    Returns an already-completed awaitable (mirrors the TS resolved ``Promise``).
    """

    async def _ready() -> None:
        return None

    return _ready()


def get_parser_module() -> ParserModule | None:
    """Always succeeds — pure-Python needs no init."""
    return _MODULE


# ───────────────────────────── Tokenizer ─────────────────────────────

TokenType = Literal[
    "WORD",
    "NUMBER",
    "OP",
    "NEWLINE",
    "COMMENT",
    "DQUOTE",
    "SQUOTE",
    "ANSI_C",
    "DOLLAR",
    "DOLLAR_PAREN",
    "DOLLAR_BRACE",
    "DOLLAR_DPAREN",
    "BACKTICK",
    "LT_PAREN",
    "GT_PAREN",
    "EOF",
]


@dataclass
class Token:
    type: str
    value: str
    # UTF-8 byte offset of first char.
    start: int
    # UTF-8 byte offset one past last char.
    end: int


SPECIAL_VARS = {"?", "$", "@", "*", "#", "-", "!", "_"}

DECL_KEYWORDS = {"export", "declare", "typeset", "readonly", "local"}

SHELL_KEYWORDS = {
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "while",
    "until",
    "for",
    "in",
    "do",
    "done",
    "case",
    "esac",
    "function",
    "select",
}


@dataclass
class HeredocPending:
    delim: str
    strip_tabs: bool
    quoted: bool
    # Filled after body scan.
    body_start: int = 0
    body_end: int = 0
    end_start: int = 0
    end_end: int = 0


@dataclass
class Lexer:
    """Lexer state. Tracks both Python-string index (``i``) and UTF-8 byte offset (``b``).

    ASCII fast path: byte == char index. Non-ASCII advances byte count per code unit. Mirrors
    the TS ``Lexer`` which operates on UTF-16 code units; here ``src`` is a Python ``str`` and
    ``i`` indexes code points, with surrogate handling retained for parity of byte counting.
    """

    src: str
    len: int
    # Python string index.
    i: int = 0
    # UTF-8 byte offset.
    b: int = 0
    # Pending heredoc delimiters awaiting body scan at next newline.
    heredocs: list[HeredocPending] = field(default_factory=list)
    # Precomputed byte offset for each char index (lazy for non-ASCII).
    byte_table: list[int] | None = None


def make_lexer(src: str) -> Lexer:
    return Lexer(src=src, len=len(src), i=0, b=0, heredocs=[], byte_table=None)


def advance(lex: Lexer) -> None:
    """Advance one char, updating byte offset for UTF-8."""
    c = ord(lex.src[lex.i])
    lex.i += 1
    if c < 0x80:
        lex.b += 1
    elif c < 0x800:
        lex.b += 2
    elif 0xD800 <= c <= 0xDBFF:
        # High surrogate — next char completes the pair, total 4 UTF-8 bytes.
        lex.b += 4
        lex.i += 1
    elif c < 0x10000:
        lex.b += 3
    else:
        # Python stores non-BMP as a single code point (4 UTF-8 bytes).
        lex.b += 4


def peek(lex: Lexer, off: int = 0) -> str:
    return lex.src[lex.i + off] if lex.i + off < lex.len else ""


def byte_at(lex: Lexer, char_idx: int) -> int:
    # Fast path: ASCII-only prefix means char idx == byte idx.
    if lex.byte_table is not None:
        return lex.byte_table[char_idx]
    # Build table on first non-trivial lookup.
    t = [0] * (lex.len + 1)
    b = 0
    i = 0
    while i < lex.len:
        t[i] = b
        c = ord(lex.src[i])
        if c < 0x80:
            b += 1
            i += 1
        elif c < 0x800:
            b += 2
            i += 1
        elif 0xD800 <= c <= 0xDBFF:
            t[i + 1] = b + 2
            b += 4
            i += 2
        elif c < 0x10000:
            b += 3
            i += 1
        else:
            b += 4
            i += 1
    t[lex.len] = b
    lex.byte_table = t
    return t[char_idx]


def is_word_char(c: str) -> bool:
    # Bash word chars: alphanumeric + punctuation that doesn't start operators.
    return (
        ("a" <= c <= "z")
        or ("A" <= c <= "Z")
        or ("0" <= c <= "9")
        or c == "_"
        or c == "/"
        or c == "."
        or c == "-"
        or c == "+"
        or c == ":"
        or c == "@"
        or c == "%"
        or c == ","
        or c == "~"
        or c == "^"
        or c == "?"
        or c == "*"
        or c == "!"
        or c == "="
        or c == "["
        or c == "]"
    )


def is_word_start(c: str) -> bool:
    return is_word_char(c) or c == "\\"


def is_ident_start(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_"


def is_ident_char(c: str) -> bool:
    return is_ident_start(c) or ("0" <= c <= "9")


def is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def is_hex_digit(c: str) -> bool:
    return is_digit(c) or ("a" <= c <= "f") or ("A" <= c <= "F")


def is_base_digit(c: str) -> bool:
    # Bash BASE#DIGITS: digits, letters, @ and _ (up to base 64).
    return is_ident_char(c) or c == "@"


def is_heredoc_delim_char(c: str) -> bool:
    """Unquoted heredoc delimiter chars. Bash accepts most non-metacharacters."""
    return (
        c != ""
        and c != " "
        and c != "\t"
        and c != "\n"
        and c != "<"
        and c != ">"
        and c != "|"
        and c != "&"
        and c != ";"
        and c != "("
        and c != ")"
        and c != "'"
        and c != '"'
        and c != "`"
        and c != "\\"
    )


def skip_blanks(lex: Lexer) -> None:
    while lex.i < lex.len:
        c = lex.src[lex.i]
        if c == " " or c == "\t" or c == "\r":
            # \r is whitespace per tree-sitter-bash extras /\s/ — handles CRLF inputs.
            advance(lex)
        elif c == "\\":
            nx = lex.src[lex.i + 1] if lex.i + 1 < lex.len else None
            if nx == "\n" or (
                nx == "\r" and lex.i + 2 < lex.len and lex.src[lex.i + 2] == "\n"
            ):
                # Line continuation — tree-sitter extras: /\\\r?\n/.
                advance(lex)
                advance(lex)
                if nx == "\r":
                    advance(lex)
            elif nx == " " or nx == "\t":
                # \<space> or \<tab> — tree-sitter's _whitespace is /\\?[ \t\v]+/.
                advance(lex)
                advance(lex)
            else:
                break
        else:
            break


_NUMBER_RE = re.compile(r"^-?\d+$")


def next_token(lex: Lexer, ctx: str = "arg") -> Token:
    """Scan next token. Context-sensitive: ``cmd`` treats ``[`` as operator (test command
    start), ``arg`` treats ``[`` as word char (glob/subscript).
    """
    skip_blanks(lex)
    start = lex.b
    if lex.i >= lex.len:
        return Token("EOF", "", start, start)

    c = lex.src[lex.i]
    c1 = peek(lex, 1)
    c2 = peek(lex, 2)

    if c == "\n":
        advance(lex)
        return Token("NEWLINE", "\n", start, lex.b)

    if c == "#":
        si = lex.i
        while lex.i < lex.len and lex.src[lex.i] != "\n":
            advance(lex)
        return Token("COMMENT", lex.src[si : lex.i], start, lex.b)

    # Multi-char operators (longest match first).
    if c == "&" and c1 == "&":
        advance(lex)
        advance(lex)
        return Token("OP", "&&", start, lex.b)
    if c == "|" and c1 == "|":
        advance(lex)
        advance(lex)
        return Token("OP", "||", start, lex.b)
    if c == "|" and c1 == "&":
        advance(lex)
        advance(lex)
        return Token("OP", "|&", start, lex.b)
    if c == ";" and c1 == ";" and c2 == "&":
        advance(lex)
        advance(lex)
        advance(lex)
        return Token("OP", ";;&", start, lex.b)
    if c == ";" and c1 == ";":
        advance(lex)
        advance(lex)
        return Token("OP", ";;", start, lex.b)
    if c == ";" and c1 == "&":
        advance(lex)
        advance(lex)
        return Token("OP", ";&", start, lex.b)
    if c == ">" and c1 == ">":
        advance(lex)
        advance(lex)
        return Token("OP", ">>", start, lex.b)
    if c == ">" and c1 == "&" and c2 == "-":
        advance(lex)
        advance(lex)
        advance(lex)
        return Token("OP", ">&-", start, lex.b)
    if c == ">" and c1 == "&":
        advance(lex)
        advance(lex)
        return Token("OP", ">&", start, lex.b)
    if c == ">" and c1 == "|":
        advance(lex)
        advance(lex)
        return Token("OP", ">|", start, lex.b)
    if c == "&" and c1 == ">" and c2 == ">":
        advance(lex)
        advance(lex)
        advance(lex)
        return Token("OP", "&>>", start, lex.b)
    if c == "&" and c1 == ">":
        advance(lex)
        advance(lex)
        return Token("OP", "&>", start, lex.b)
    if c == "<" and c1 == "<" and c2 == "<":
        advance(lex)
        advance(lex)
        advance(lex)
        return Token("OP", "<<<", start, lex.b)
    if c == "<" and c1 == "<" and c2 == "-":
        advance(lex)
        advance(lex)
        advance(lex)
        return Token("OP", "<<-", start, lex.b)
    if c == "<" and c1 == "<":
        advance(lex)
        advance(lex)
        return Token("OP", "<<", start, lex.b)
    if c == "<" and c1 == "&" and c2 == "-":
        advance(lex)
        advance(lex)
        advance(lex)
        return Token("OP", "<&-", start, lex.b)
    if c == "<" and c1 == "&":
        advance(lex)
        advance(lex)
        return Token("OP", "<&", start, lex.b)
    if c == "<" and c1 == "(":
        advance(lex)
        advance(lex)
        return Token("LT_PAREN", "<(", start, lex.b)
    if c == ">" and c1 == "(":
        advance(lex)
        advance(lex)
        return Token("GT_PAREN", ">(", start, lex.b)
    if c == "(" and c1 == "(":
        advance(lex)
        advance(lex)
        return Token("OP", "((", start, lex.b)
    if c == ")" and c1 == ")":
        advance(lex)
        advance(lex)
        return Token("OP", "))", start, lex.b)

    if c == "|" or c == "&" or c == ";" or c == ">" or c == "<":
        advance(lex)
        return Token("OP", c, start, lex.b)
    if c == "(" or c == ")":
        advance(lex)
        return Token("OP", c, start, lex.b)

    # In cmd position, [ [[ { start test/group; in arg position they're word chars.
    if ctx == "cmd":
        if c == "[" and c1 == "[":
            advance(lex)
            advance(lex)
            return Token("OP", "[[", start, lex.b)
        if c == "[":
            advance(lex)
            return Token("OP", "[", start, lex.b)
        if c == "{" and (c1 == " " or c1 == "\t" or c1 == "\n"):
            advance(lex)
            return Token("OP", "{", start, lex.b)
        if c == "}":
            advance(lex)
            return Token("OP", "}", start, lex.b)
        if c == "!" and (c1 == " " or c1 == "\t"):
            advance(lex)
            return Token("OP", "!", start, lex.b)

    if c == '"':
        advance(lex)
        return Token("DQUOTE", '"', start, lex.b)
    if c == "'":
        si = lex.i
        advance(lex)
        while lex.i < lex.len and lex.src[lex.i] != "'":
            advance(lex)
        if lex.i < lex.len:
            advance(lex)
        return Token("SQUOTE", lex.src[si : lex.i], start, lex.b)

    if c == "$":
        if c1 == "(" and c2 == "(":
            advance(lex)
            advance(lex)
            advance(lex)
            return Token("DOLLAR_DPAREN", "$((", start, lex.b)
        if c1 == "(":
            advance(lex)
            advance(lex)
            return Token("DOLLAR_PAREN", "$(", start, lex.b)
        if c1 == "{":
            advance(lex)
            advance(lex)
            return Token("DOLLAR_BRACE", "${", start, lex.b)
        if c1 == "'":
            # ANSI-C string $'...'.
            si = lex.i
            advance(lex)
            advance(lex)
            while lex.i < lex.len and lex.src[lex.i] != "'":
                if lex.src[lex.i] == "\\" and lex.i + 1 < lex.len:
                    advance(lex)
                advance(lex)
            if lex.i < lex.len:
                advance(lex)
            return Token("ANSI_C", lex.src[si : lex.i], start, lex.b)
        advance(lex)
        return Token("DOLLAR", "$", start, lex.b)

    if c == "`":
        advance(lex)
        return Token("BACKTICK", "`", start, lex.b)

    # File descriptor before redirect: digit+ immediately followed by > or <.
    if is_digit(c):
        j = lex.i
        while j < lex.len and is_digit(lex.src[j]):
            j += 1
        after = lex.src[j] if j < lex.len else ""
        if after == ">" or after == "<":
            si = lex.i
            while lex.i < j:
                advance(lex)
            return Token("WORD", lex.src[si : lex.i], start, lex.b)

    # Word / number.
    if is_word_start(c) or c == "{" or c == "}":
        si = lex.i
        while lex.i < lex.len:
            ch = lex.src[lex.i]
            if ch == "\\":
                if lex.i + 1 >= lex.len:
                    # Trailing `\` at EOF — tree-sitter excludes it; stop here.
                    break
                # Escape next char (including \n for line continuation mid-word).
                if lex.src[lex.i + 1] == "\n":
                    advance(lex)
                    advance(lex)
                    continue
                advance(lex)
                advance(lex)
                continue
            if not is_word_char(ch) and ch != "{" and ch != "}":
                break
            advance(lex)
        if lex.i > si:
            v = lex.src[si : lex.i]
            # Number: optional sign then digits only.
            if _NUMBER_RE.match(v):
                return Token("NUMBER", v, start, lex.b)
            return Token("WORD", v, start, lex.b)
        # Empty word (lone `\` at EOF) — fall through to single-char consumer.

    # Unknown char — consume as single-char word.
    advance(lex)
    return Token("WORD", c, start, lex.b)


# ───────────────────────────── Parser ─────────────────────────────


@dataclass
class ParseState:
    lex: Lexer
    src: str
    src_bytes: int
    # True when byte offsets == char indices (no multi-byte UTF-8).
    is_ascii: bool
    node_count: int = 0
    deadline: float = 0.0
    aborted: bool = False
    # Depth of backtick nesting — inside `...`, ` terminates words.
    in_backtick: int = 0
    # When set, parse_simple_command stops at this token (for `[` backtrack).
    stop_token: str | None = None


class _BudgetExceededError(Exception):
    """Internal control-flow signal — node budget or timeout hit (matches TS throw)."""


def parse_source(source: str, timeout_ms: float | None = None) -> TsNode | None:
    lex = make_lexer(source)
    src_bytes = byte_length_utf8(source)
    p = ParseState(
        lex=lex,
        src=source,
        src_bytes=src_bytes,
        is_ascii=src_bytes == len(source),
        node_count=0,
        deadline=_now_ms() + (timeout_ms if timeout_ms is not None else PARSE_TIMEOUT_MS),
        aborted=False,
        in_backtick=0,
        stop_token=None,
    )
    try:
        program = parse_program(p)
        if p.aborted:
            return None
        return program
    except Exception:
        # Mirrors the TS `try { ... } catch { return null }` — any parse failure
        return None


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def byte_length_utf8(s: str) -> int:
    b = 0
    i = 0
    n = len(s)
    while i < n:
        c = ord(s[i])
        if c < 0x80:
            b += 1
        elif c < 0x800:
            b += 2
        elif 0xD800 <= c <= 0xDBFF:
            b += 4
            i += 1
        elif c < 0x10000:
            b += 3
        else:
            b += 4
        i += 1
    return b


def check_budget(p: ParseState) -> None:
    p.node_count += 1
    if p.node_count > MAX_NODES:
        p.aborted = True
        raise _BudgetExceededError("budget")
    if (p.node_count & 0x7F) == 0 and _now_ms() > p.deadline:
        p.aborted = True
        raise _BudgetExceededError("timeout")


def mk(p: ParseState, type_: str, start: int, end: int, children: list[TsNode]) -> TsNode:
    """Build a node. Slices text from source by byte range via char-index lookup."""
    check_budget(p)
    return {
        "type": type_,
        "text": slice_bytes(p, start, end),
        "startIndex": start,
        "endIndex": end,
        "children": children,
    }


def slice_bytes(p: ParseState, start_byte: int, end_byte: int) -> str:
    if p.is_ascii:
        return p.src[start_byte:end_byte]
    # Find char indices for byte offsets. Build byte table if needed.
    lex = p.lex
    if lex.byte_table is None:
        byte_at(lex, 0)
    t = lex.byte_table
    assert t is not None
    # Binary search for char index where byte offset matches.
    lo = 0
    hi = len(p.src)
    while lo < hi:
        m = (lo + hi) >> 1
        if t[m] < start_byte:
            lo = m + 1
        else:
            hi = m
    sc = lo
    lo = sc
    hi = len(p.src)
    while lo < hi:
        m = (lo + hi) >> 1
        if t[m] < end_byte:
            lo = m + 1
        else:
            hi = m
    return p.src[sc:lo]


def leaf(p: ParseState, type_: str, tok: Token) -> TsNode:
    return mk(p, type_, tok.start, tok.end, [])


def parse_program(p: ParseState) -> TsNode:
    children: list[TsNode] = []
    # Skip leading whitespace & newlines — program start is first content byte.
    skip_blanks(p.lex)
    while True:
        save = save_lex(p.lex)
        t = next_token(p.lex, "cmd")
        if t.type == "NEWLINE":
            skip_blanks(p.lex)
            continue
        restore_lex(p.lex, save)
        break
    prog_start = p.lex.b
    while p.lex.i < p.lex.len:
        save = save_lex(p.lex)
        t = next_token(p.lex, "cmd")
        if t.type == "EOF":
            break
        if t.type == "NEWLINE":
            continue
        if t.type == "COMMENT":
            children.append(leaf(p, "comment", t))
            continue
        restore_lex(p.lex, save)
        stmts = parse_statements(p, None)
        for s in stmts:
            children.append(s)
        if len(stmts) == 0:
            # Couldn't parse — emit ERROR and skip one token.
            err_tok = next_token(p.lex, "cmd")
            if err_tok.type == "EOF":
                break
            # Stray `;;` at program level — tree-sitter silently elides. Keep leading
            # `;` as ERROR (security: paste artifact).
            if err_tok.type == "OP" and err_tok.value == ";;" and len(children) > 0:
                continue
            children.append(mk(p, "ERROR", err_tok.start, err_tok.end, []))
    # tree-sitter includes trailing whitespace in program extent.
    prog_end = p.src_bytes if len(children) > 0 else prog_start
    return mk(p, "program", prog_start, prog_end, children)


# Packed as (b << 16) | i — avoids heap alloc on every backtrack.
def save_lex(lex: Lexer) -> int:
    return lex.b * 0x10000 + lex.i


def restore_lex(lex: Lexer, s: int) -> None:
    lex.i = s & 0xFFFF
    lex.b = s >> 16


def parse_statements(p: ParseState, terminator: str | None) -> list[TsNode]:
    """Parse a sequence of statements separated by ; & newline. Returns a flat list where ;
    and & are sibling leaves (NOT wrapped in 'list' — only && || get that). Stops at
    terminator or EOF.
    """
    out: list[TsNode] = []
    while True:
        skip_blanks(p.lex)
        save = save_lex(p.lex)
        t = next_token(p.lex, "cmd")
        if t.type == "EOF":
            restore_lex(p.lex, save)
            break
        if t.type == "NEWLINE":
            # Process pending heredocs.
            if len(p.lex.heredocs) > 0:
                scan_heredoc_bodies(p)
            continue
        if t.type == "COMMENT":
            out.append(leaf(p, "comment", t))
            continue
        if terminator and t.type == "OP" and t.value == terminator:
            restore_lex(p.lex, save)
            break
        if t.type == "OP" and t.value in (
            ")",
            "}",
            ";;",
            ";&",
            ";;&",
            "))",
            "]]",
            "]",
        ):
            restore_lex(p.lex, save)
            break
        if t.type == "BACKTICK" and p.in_backtick > 0:
            restore_lex(p.lex, save)
            break
        if t.type == "WORD" and t.value in (
            "then",
            "elif",
            "else",
            "fi",
            "do",
            "done",
            "esac",
        ):
            restore_lex(p.lex, save)
            break
        restore_lex(p.lex, save)
        stmt = parse_and_or(p)
        if not stmt:
            break
        out.append(stmt)
        # Look for separator.
        skip_blanks(p.lex)
        save2 = save_lex(p.lex)
        sep = next_token(p.lex, "cmd")
        if sep.type == "OP" and (sep.value == ";" or sep.value == "&"):
            # Check if terminator follows — if so, emit separator but stop.
            save3 = save_lex(p.lex)
            after = next_token(p.lex, "cmd")
            restore_lex(p.lex, save3)
            out.append(leaf(p, sep.value, sep))
            if (
                after.type == "EOF"
                or (
                    after.type == "OP"
                    and after.value in (")", "}", ";;", ";&", ";;&")
                )
                or (
                    after.type == "WORD"
                    and after.value
                    in ("then", "elif", "else", "fi", "do", "done", "esac")
                )
            ):
                continue
        elif sep.type == "NEWLINE":
            if len(p.lex.heredocs) > 0:
                scan_heredoc_bodies(p)
            continue
        else:
            restore_lex(p.lex, save2)
    return out


def parse_and_or(p: ParseState) -> TsNode | None:
    left = parse_pipeline(p)
    if not left:
        return None
    while True:
        save = save_lex(p.lex)
        t = next_token(p.lex, "cmd")
        if t.type == "OP" and (t.value == "&&" or t.value == "||"):
            op = leaf(p, t.value, t)
            skip_newlines(p)
            right = parse_pipeline(p)
            if not right:
                left = mk(p, "list", left["startIndex"], op["endIndex"], [left, op])
                break
            # If right is a redirected_statement, hoist its redirects to wrap the list.
            if right["type"] == "redirected_statement" and len(right["children"]) >= 2:
                inner = right["children"][0]
                redirs = right["children"][1:]
                list_node = mk(
                    p, "list", left["startIndex"], inner["endIndex"], [left, op, inner]
                )
                last_r = redirs[-1]
                left = mk(
                    p,
                    "redirected_statement",
                    list_node["startIndex"],
                    last_r["endIndex"],
                    [list_node, *redirs],
                )
            else:
                left = mk(
                    p, "list", left["startIndex"], right["endIndex"], [left, op, right]
                )
        else:
            restore_lex(p.lex, save)
            break
    return left


def skip_newlines(p: ParseState) -> None:
    while True:
        save = save_lex(p.lex)
        t = next_token(p.lex, "cmd")
        if t.type != "NEWLINE":
            restore_lex(p.lex, save)
            break


# ─────────────────────── Arithmetic precedence tables ───────────────────────

ArithMode = Literal["var", "word", "assign"]

# Operator precedence table (higher = tighter binding).
ARITH_PREC: dict[str, int] = {
    "=": 2,
    "+=": 2,
    "-=": 2,
    "*=": 2,
    "/=": 2,
    "%=": 2,
    "<<=": 2,
    ">>=": 2,
    "&=": 2,
    "^=": 2,
    "|=": 2,
    "||": 4,
    "&&": 5,
    "|": 6,
    "^": 7,
    "&": 8,
    "==": 9,
    "!=": 9,
    "<": 10,
    ">": 10,
    "<=": 10,
    ">=": 10,
    "<<": 11,
    ">>": 11,
    "+": 12,
    "-": 12,
    "*": 13,
    "/": 13,
    "%": 13,
    "**": 14,
}

# Right-associative operators (assignment and exponent).
ARITH_RIGHT_ASSOC = {
    "=",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "<<=",
    ">>=",
    "&=",
    "^=",
    "|=",
    "**",
}


# The deep recursive-descent grammar (commands, redirects, words,
# expansions, test/arith expressions, heredoc bodies — `parse_pipeline` and everything it
# transitively calls) is not implemented in this build. It is DEAD AT RUNTIME because the
# parser is gated off (parse_command_raw / parse_command always return None), so these stubs are
# never reached. Each raises NotImplementedError, which parse_source() catches and returns None
# (fail-closed), preserving the gated-off behavior. The exported surface (TsNode, SHELL_KEYWORDS,
# ensure_parser_initialized, get_parser_module, parse_source) and the top-of-grammar
# (parse_program / parse_statements / parse_and_or) are implemented above.


def _not_implemented(_name: str) -> None:
    raise NotImplementedError(
        f"{_name} — deep bash grammar is not implemented in this build (parser is gated off)"
    )


def parse_pipeline(p: ParseState) -> TsNode | None:
    _not_implemented("parse_pipeline")
    return None


def parse_command(p: ParseState) -> TsNode | None:
    _not_implemented("parse_command")
    return None


def parse_simple_command(p: ParseState) -> TsNode | None:
    _not_implemented("parse_simple_command")
    return None


def scan_heredoc_bodies(p: ParseState) -> None:
    _not_implemented("scan_heredoc_bodies")


def parse_word(p: ParseState, _ctx: str) -> TsNode | None:
    _not_implemented("parse_word")
    return None


def try_parse_redirect(p: ParseState, greedy: bool = False) -> TsNode | None:
    _not_implemented("try_parse_redirect")
    return None


def parse_double_quoted(p: ParseState) -> TsNode:
    _not_implemented("parse_double_quoted")
    raise AssertionError("unreachable")


def parse_dollar_like(p: ParseState) -> TsNode | None:
    _not_implemented("parse_dollar_like")
    return None


def parse_backtick(p: ParseState) -> TsNode | None:
    _not_implemented("parse_backtick")
    return None


def parse_if(p: ParseState, if_tok: Token) -> TsNode:
    _not_implemented("parse_if")
    raise AssertionError("unreachable")


def parse_while(p: ParseState, kw_tok: Token) -> TsNode:
    _not_implemented("parse_while")
    raise AssertionError("unreachable")


def parse_for(p: ParseState, for_tok: Token) -> TsNode:
    _not_implemented("parse_for")
    raise AssertionError("unreachable")


def parse_case(p: ParseState, case_tok: Token) -> TsNode:
    _not_implemented("parse_case")
    raise AssertionError("unreachable")


def parse_function(p: ParseState, fn_tok: Token) -> TsNode:
    _not_implemented("parse_function")
    raise AssertionError("unreachable")


def parse_declaration(p: ParseState, kw_tok: Token) -> TsNode:
    _not_implemented("parse_declaration")
    raise AssertionError("unreachable")


def parse_test_expr(p: ParseState, closer: str) -> TsNode | None:
    _not_implemented("parse_test_expr")
    return None


def parse_arith_expr(p: ParseState, stop: str, mode: ArithMode = "word") -> TsNode | None:
    _not_implemented("parse_arith_expr")
    return None


# The parser module facade (`{ parse }`). `parse` is the gated entry the TS `parser.ts` wraps.
_MODULE = ParserModule(parse=parse_source)
