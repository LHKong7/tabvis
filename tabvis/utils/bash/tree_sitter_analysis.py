"""Tree-sitter AST analysis utilities for bash command security validation.

Analyze shell commands through tree-sitter AST nodes.

These functions extract security-relevant information from tree-sitter parse trees, providing
more accurate analysis than regex/shell-quote parsing. Each function takes a root node and the
command string and returns structured data used by security validators.

The native NAPI parser returns plain JS objects — no cleanup needed.

AST node shape (a plain dict): ``{type, text, startIndex (UTF-8 BYTE offset),
endIndex (UTF-8 byte offset), children, childCount}``. ``startIndex``/``endIndex`` are UTF-8
**byte** offsets (as in tree-sitter), so the offset-based string surgery below operates on the
UTF-8 byte representation of ``command`` and decodes back — matching ``ParsedCommand.ts``'s
deliberate ``Buffer``-slicing. For ASCII inputs byte offset == str index, so the two coincide.
"""

from __future__ import annotations

from typing import Any, TypedDict

# A tree-sitter node is a plain dict; field names kept verbatim from the TS contract.
TreeSitterNode = dict[str, Any]


class QuoteContext(TypedDict):
    # Command text with single-quoted content removed (double-quoted content preserved)
    withDoubleQuotes: str
    # Command text with all quoted content removed
    fullyUnquoted: str
    # Like fullyUnquoted but preserves quote characters (', ")
    unquotedKeepQuoteChars: str


class CompoundStructure(TypedDict):
    # Whether the command has compound operators (&&, ||, ;) at the top level
    hasCompoundOperators: bool
    # Whether the command has pipelines
    hasPipeline: bool
    # Whether the command has subshells
    hasSubshell: bool
    # Whether the command has command groups ({...})
    hasCommandGroup: bool
    # Top-level compound operator types found
    operators: list[str]
    # Individual command segments split by compound operators
    segments: list[str]


class DangerousPatterns(TypedDict):
    # Has $() or backtick command substitution
    hasCommandSubstitution: bool
    # Has <() or >() process substitution
    hasProcessSubstitution: bool
    # Has ${...} parameter expansion
    hasParameterExpansion: bool
    # Has heredoc
    hasHeredoc: bool
    # Has comment
    hasComment: bool


class TreeSitterAnalysis(TypedDict):
    quoteContext: QuoteContext
    compoundStructure: CompoundStructure
    # Whether actual operator nodes (;, &&, ||) exist — if false, \; is just a word argument
    hasActualOperatorNodes: bool
    dangerousPatterns: DangerousPatterns


# QuoteSpans: byte-offset [start, end] spans grouped by quote kind.
#   raw    -> raw_string (single-quoted)
#   ansiC  -> ansi_c_string ($'...')
#   double -> string (double-quoted)
#   heredoc -> quoted heredoc_redirect
class _QuoteSpans(TypedDict):
    raw: list[tuple[int, int]]
    ansiC: list[tuple[int, int]]
    double: list[tuple[int, int]]
    heredoc: list[tuple[int, int]]


def _children(node: TreeSitterNode) -> list[TreeSitterNode]:
    return node.get("children") or []


def _collect_quote_spans(node: TreeSitterNode, out: _QuoteSpans, in_double: bool) -> None:
    """Single-pass collection of all quote-related spans.

    Replicates the per-type walk semantics: ``raw_string`` / ``ansi_c_string`` /
    quoted-heredoc bodies are literal text in bash (no expansion) → return early. ``string``
    only records the *outermost* span (tracked via ``in_double``) but still recurses to pick up
    nested ``raw_string``/``ansi_c_string`` inside ``$(...)``/``${...}``.
    """
    node_type = node.get("type")
    if node_type == "raw_string":
        out["raw"].append((node["startIndex"], node["endIndex"]))
        return  # literal body, no nested quotes possible
    if node_type == "ansi_c_string":
        out["ansiC"].append((node["startIndex"], node["endIndex"]))
        return  # literal body
    if node_type == "string":
        # Only collect the outermost string. Recurse regardless — a nested
        # $(cmd 'x') inside "..." has a real inner raw_string.
        if not in_double:
            out["double"].append((node["startIndex"], node["endIndex"]))
        for child in _children(node):
            if child:
                _collect_quote_spans(child, out, True)
        return
    if node_type == "heredoc_redirect":
        # Quoted heredocs (<<'EOF', <<"EOF", <<\EOF): literal body.
        # Detection: heredoc_start text starts with '/"/\\
        is_quoted = False
        for child in _children(node):
            if child and child.get("type") == "heredoc_start":
                text = child.get("text") or ""
                first = text[0] if text else ""
                is_quoted = first in ("'", '"', "\\")
                break
        if is_quoted:
            out["heredoc"].append((node["startIndex"], node["endIndex"]))
            return  # literal body, no nested quote nodes
        # Unquoted: fall through to recurse into the heredoc body.

    for child in _children(node):
        if child:
            _collect_quote_spans(child, out, in_double)


def _build_position_set(spans: list[tuple[int, int]]) -> set[int]:
    """Builds a set of all byte positions covered by the given spans."""
    out: set[int] = set()
    for start, end in spans:
        out.update(range(start, end))
    return out


def _drop_contained_spans(spans: list[Any]) -> list[Any]:
    """Drop spans fully contained within another span, keeping only the outermost.

    Each element is a tuple whose first two members are
    ``(start, end)``; extra trailing members (delimiters) are ignored by the comparison.
    """
    result: list[Any] = []
    for i, s in enumerate(spans):
        contained = False
        for j, other in enumerate(spans):
            if j == i:
                continue
            if (
                other[0] <= s[0]
                and other[1] >= s[1]
                and (other[0] < s[0] or other[1] > s[1])
            ):
                contained = True
                break
        if not contained:
            result.append(s)
    return result


def _remove_spans(command_bytes: bytes, spans: list[tuple[int, int]]) -> bytes:
    """Removes spans (byte ranges) from ``command_bytes``."""
    if not spans:
        return command_bytes
    sorted_spans = sorted(_drop_contained_spans(list(spans)), key=lambda s: s[0], reverse=True)
    result = command_bytes
    for start, end in sorted_spans:
        result = result[:start] + result[end:]
    return result


def _replace_spans_keep_quotes(
    command_bytes: bytes, spans: list[tuple[int, int, str, str]]
) -> bytes:
    """Replace spans with just their quote delimiters."""
    if not spans:
        return command_bytes
    sorted_spans = sorted(_drop_contained_spans(list(spans)), key=lambda s: s[0], reverse=True)
    result = command_bytes
    for start, end, open_, close in sorted_spans:
        result = result[:start] + open_.encode("utf-8") + close.encode("utf-8") + result[end:]
    return result


def extract_quote_context(root_node: Any, command: str) -> QuoteContext:
    """Extract quote context from the tree-sitter AST.

    Node types: ``raw_string`` (single-quoted), ``string`` (double-quoted), ``ansi_c_string``
    (``$'...'`` — span includes the leading ``$``), and QUOTED ``heredoc_redirect`` (the full
    redirect span is stripped; unquoted heredocs are left in place).
    """
    command_bytes = command.encode("utf-8")
    spans: _QuoteSpans = {"raw": [], "ansiC": [], "double": [], "heredoc": []}
    _collect_quote_spans(root_node, spans, False)
    single_quote_spans = spans["raw"]
    ansi_c_spans = spans["ansiC"]
    double_quote_spans = spans["double"]
    quoted_heredoc_spans = spans["heredoc"]
    all_quote_spans = [
        *single_quote_spans,
        *ansi_c_spans,
        *double_quote_spans,
        *quoted_heredoc_spans,
    ]

    # withDoubleQuotes: drop single-quoted spans entirely plus the opening/closing `"`
    # delimiters of double-quoted spans (keep the content between them).
    single_quote_set = _build_position_set(
        [*single_quote_spans, *ansi_c_spans, *quoted_heredoc_spans]
    )
    double_quote_delim_set: set[int] = set()
    for start, end in double_quote_spans:
        double_quote_delim_set.add(start)  # opening "
        double_quote_delim_set.add(end - 1)  # closing "

    kept = bytearray()
    for i in range(len(command_bytes)):
        if i in single_quote_set:
            continue
        if i in double_quote_delim_set:
            continue
        kept.append(command_bytes[i])
    with_double_quotes = bytes(kept).decode("utf-8", "replace")

    # fullyUnquoted: remove all quoted content.
    fully_unquoted = _remove_spans(command_bytes, all_quote_spans).decode("utf-8", "replace")

    # unquotedKeepQuoteChars: remove content but keep the delimiter chars.
    spans_with_quote_chars: list[tuple[int, int, str, str]] = []
    for start, end in single_quote_spans:
        spans_with_quote_chars.append((start, end, "'", "'"))
    for start, end in ansi_c_spans:
        # ansi_c_string spans include the leading $; preserve it.
        spans_with_quote_chars.append((start, end, "$'", "'"))
    for start, end in double_quote_spans:
        spans_with_quote_chars.append((start, end, '"', '"'))
    for start, end in quoted_heredoc_spans:
        # Heredoc redirect spans have no inline quote delimiters — strip entirely.
        spans_with_quote_chars.append((start, end, "", ""))
    unquoted_keep_quote_chars = _replace_spans_keep_quotes(
        command_bytes, spans_with_quote_chars
    ).decode("utf-8", "replace")

    return {
        "withDoubleQuotes": with_double_quotes,
        "fullyUnquoted": fully_unquoted,
        "unquotedKeepQuoteChars": unquoted_keep_quote_chars,
    }


def extract_compound_structure(root_node: Any, command: str) -> CompoundStructure:
    """Extract compound command structure from the AST."""
    operators: list[str] = []
    segments: list[str] = []
    state = {"hasSubshell": False, "hasCommandGroup": False, "hasPipeline": False}

    def walk_top_level(node: TreeSitterNode) -> None:
        for child in _children(node):
            if not child:
                continue
            ctype = child.get("type")

            if ctype == "list":
                # list nodes contain && and || operators
                for list_child in _children(child):
                    if not list_child:
                        continue
                    lct = list_child.get("type")
                    if lct in ("&&", "||"):
                        operators.append(lct)
                    elif lct in ("list", "redirected_statement"):
                        # Nested list / redirected_statement wrapping a list/pipeline — recurse.
                        walk_top_level({**node, "children": [list_child]})
                    elif lct == "pipeline":
                        state["hasPipeline"] = True
                        segments.append(list_child.get("text", ""))
                    elif lct == "subshell":
                        state["hasSubshell"] = True
                        segments.append(list_child.get("text", ""))
                    elif lct == "compound_statement":
                        state["hasCommandGroup"] = True
                        segments.append(list_child.get("text", ""))
                    else:
                        segments.append(list_child.get("text", ""))
            elif ctype == ";":
                operators.append(";")
            elif ctype == "pipeline":
                state["hasPipeline"] = True
                segments.append(child.get("text", ""))
            elif ctype == "subshell":
                state["hasSubshell"] = True
                segments.append(child.get("text", ""))
            elif ctype == "compound_statement":
                state["hasCommandGroup"] = True
                segments.append(child.get("text", ""))
            elif ctype in ("command", "declaration_command", "variable_assignment"):
                segments.append(child.get("text", ""))
            elif ctype == "redirected_statement":
                # tree-sitter wraps the ENTIRE compound in a redirected_statement; recurse to
                # detect the inner structure, skipping file_redirect children.
                found_inner = False
                for inner in _children(child):
                    if not inner or inner.get("type") == "file_redirect":
                        continue
                    found_inner = True
                    walk_top_level({**child, "children": [inner]})
                if not found_inner:
                    segments.append(child.get("text", ""))
            elif ctype == "negated_command":
                # `! cmd` — record the full negated text, then recurse into the inner command.
                segments.append(child.get("text", ""))
                walk_top_level(child)
            elif ctype in (
                "if_statement",
                "while_statement",
                "for_statement",
                "case_statement",
                "function_definition",
            ):
                # Control-flow constructs: one segment, but recurse for inner structure.
                segments.append(child.get("text", ""))
                walk_top_level(child)

    walk_top_level(root_node)

    # If no segments found, the whole command is one segment.
    if not segments:
        segments.append(command)

    return {
        "hasCompoundOperators": len(operators) > 0,
        "hasPipeline": state["hasPipeline"],
        "hasSubshell": state["hasSubshell"],
        "hasCommandGroup": state["hasCommandGroup"],
        "operators": operators,
        "segments": segments,
    }


def has_actual_operator_nodes(root_node: Any) -> bool:
    """Check whether the AST contains actual operator nodes.

    Eliminates the ``find -exec \\;`` false positive: tree-sitter parses ``\\;`` as part of a
    ``word`` node (an argument), NOT a ``;`` operator. If no actual ``;`` operator nodes exist,
    there are no compound operators.
    """

    def walk(node: TreeSitterNode) -> bool:
        ntype = node.get("type")
        if ntype in (";", "&&", "||"):
            return True
        if ntype == "list":
            return True
        for child in _children(node):
            if child and walk(child):
                return True
        return False

    return walk(root_node)


def extract_dangerous_patterns(root_node: Any) -> DangerousPatterns:
    """Extract dangerous pattern information from the AST."""
    found = {
        "hasCommandSubstitution": False,
        "hasProcessSubstitution": False,
        "hasParameterExpansion": False,
        "hasHeredoc": False,
        "hasComment": False,
    }

    def walk(node: TreeSitterNode) -> None:
        ntype = node.get("type")
        if ntype == "command_substitution":
            found["hasCommandSubstitution"] = True
        elif ntype == "process_substitution":
            found["hasProcessSubstitution"] = True
        elif ntype == "expansion":
            found["hasParameterExpansion"] = True
        elif ntype == "heredoc_redirect":
            found["hasHeredoc"] = True
        elif ntype == "comment":
            found["hasComment"] = True

        for child in _children(node):
            if child:
                walk(child)

    walk(root_node)
    return {
        "hasCommandSubstitution": found["hasCommandSubstitution"],
        "hasProcessSubstitution": found["hasProcessSubstitution"],
        "hasParameterExpansion": found["hasParameterExpansion"],
        "hasHeredoc": found["hasHeredoc"],
        "hasComment": found["hasComment"],
    }


def analyze_command(root_node: Any, command: str) -> TreeSitterAnalysis:
    """Perform complete tree-sitter analysis of a command.

    Extracts all security-relevant data from the AST in one pass.
    """
    return {
        "quoteContext": extract_quote_context(root_node, command),
        "compoundStructure": extract_compound_structure(root_node, command),
        "hasActualOperatorNodes": has_actual_operator_nodes(root_node),
        "dangerousPatterns": extract_dangerous_patterns(root_node),
    }


__all__ = [
    "CompoundStructure",
    "DangerousPatterns",
    "QuoteContext",
    "TreeSitterAnalysis",
    "TreeSitterNode",
    "analyze_command",
    "extract_compound_structure",
    "extract_dangerous_patterns",
    "extract_quote_context",
    "has_actual_operator_nodes",
]
