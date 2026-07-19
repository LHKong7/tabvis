"""Parsed-command abstraction

Two implementations conform to the :class:`IParsedCommand` protocol:
- :class:`RegexParsedCommand_DEPRECATED` — the regex / shell-quote FALLBACK (what runs at runtime,
  since the tree-sitter parser is gated off — see memory ``bash-parser-gated-off``).
- :class:`TreeSitterParsedCommand` — the AST-backed implementation (dead at runtime; built only from
  a pre-parsed AST root via :func:`build_parsed_command_from_root`).

``ParsedCommand.parse`` checks tree-sitter availability via the gated
:func:`tabvis.utils.bash.parser.parse_command` (always ``None``), so it always returns a
:class:`RegexParsedCommand_DEPRECATED` at runtime.

Implementation notes:
- Tree-sitter ``startIndex`` / ``endIndex`` are **UTF-8 BYTE offsets**, but Python ``str`` slicing is
  by code point. As in the TS (which slices a ``Buffer``), the AST path slices the UTF-8 *bytes* of
  the command with those offsets and decodes back — correct for multi-byte code points. Plain data
  nodes stay dicts (``{type, text, startIndex, endIndex, children}`` — field names verbatim).
- ``lodash memoize`` (async ``getTreeSitterAvailable``) → a module-level single-shot cache.
- ``OutputRedirection`` / the redirection node are plain dicts with verbatim wire keys
  (``target`` / ``operator`` / ``startIndex`` / ``endIndex``).
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from tabvis.utils.bash.commands import (
    extract_output_redirections,
    split_command_with_operators,
)
from tabvis.utils.bash.parser import Node
from tabvis.utils.bash.tree_sitter_analysis import (
    TreeSitterAnalysis,
    analyze_command,
)

# OutputRedirection is a plain dict: {'target': str, 'operator': '>' | '>>'} (wire keys verbatim).
OutputRedirection = dict[str, str]

_WHITESPACE_RUN_RE = re.compile(r"\s+")


class IParsedCommand(Protocol):
    """Interface both tree-sitter and regex fallback implementations conform to."""

    original_command: str

    def to_string(self) -> str: ...
    def get_pipe_segments(self) -> list[str]: ...
    def without_output_redirections(self) -> str: ...
    def get_output_redirections(self) -> list[OutputRedirection]: ...
    def get_tree_sitter_analysis(self) -> TreeSitterAnalysis | None: ...


class RegexParsedCommand_DEPRECATED:  # noqa: N801 — verbatim TS export name (legacy/test marker)
    """Regex/shell-quote fallback (used when tree-sitter is unavailable).

    Deprecated: the primary gate is ``parse_for_security`` (ast.ts). Exported for testing.
    """

    def __init__(self, command: str) -> None:
        self.original_command = command

    def to_string(self) -> str:
        return self.original_command

    def get_pipe_segments(self) -> list[str]:
        try:
            parts = split_command_with_operators(self.original_command)
            segments: list[str] = []
            current_segment: list[str] = []

            for part in parts:
                if part == "|":
                    if len(current_segment) > 0:
                        segments.append(" ".join(current_segment))
                        current_segment = []
                else:
                    current_segment.append(part)

            if len(current_segment) > 0:
                segments.append(" ".join(current_segment))

            return segments if len(segments) > 0 else [self.original_command]
        except Exception:  # noqa: BLE001 — faithful to the TS bare catch
            return [self.original_command]

    def without_output_redirections(self) -> str:
        if ">" not in self.original_command:
            return self.original_command
        result = extract_output_redirections(self.original_command)
        return (
            result["commandWithoutRedirections"]
            if len(result["redirections"]) > 0
            else self.original_command
        )

    def get_output_redirections(self) -> list[OutputRedirection]:
        result = extract_output_redirections(self.original_command)
        return result["redirections"]

    def get_tree_sitter_analysis(self) -> TreeSitterAnalysis | None:
        return None


# RedirectionNode = OutputRedirection & { startIndex: number; endIndex: number } — a plain dict.
RedirectionNode = dict[str, Any]


def _visit_nodes(node: Node, visitor: Any) -> None:
    visitor(node)
    for child in node["children"]:
        _visit_nodes(child, visitor)


def _extract_pipe_positions(root_node: Node) -> list[int]:
    pipe_positions: list[int] = []

    def _visit(node: Node) -> None:
        if node["type"] == "pipeline":
            for child in node["children"]:
                if child["type"] == "|":
                    pipe_positions.append(child["startIndex"])

    _visit_nodes(root_node, _visit)
    # visit_nodes is depth-first; for `a | b && c | d` the outer `|` is visited before the inner,
    # so positions arrive out of order. get_pipe_segments slices left-to-right, so sort here.
    return sorted(pipe_positions)


def _extract_redirection_nodes(root_node: Node) -> list[RedirectionNode]:
    redirections: list[RedirectionNode] = []

    def _visit(node: Node) -> None:
        if node["type"] == "file_redirect":
            children = node["children"]
            op = next((c for c in children if c["type"] in (">", ">>")), None)
            target = next((c for c in children if c["type"] == "word"), None)
            if op and target:
                redirections.append(
                    {
                        "startIndex": node["startIndex"],
                        "endIndex": node["endIndex"],
                        "target": target["text"],
                        "operator": op["type"],
                    }
                )

    _visit_nodes(root_node, _visit)
    return redirections


class TreeSitterParsedCommand:
    """AST-backed :class:`IParsedCommand` (dead at runtime — built from a pre-parsed root).

    ``startIndex`` / ``endIndex`` are UTF-8 byte offsets, so this slices ``command_bytes`` (the
    UTF-8 encoding of the command) and decodes back — correct regardless of code-point width.
    """

    def __init__(
        self,
        command: str,
        pipe_positions: list[int],
        redirection_nodes: list[RedirectionNode],
        tree_sitter_analysis: TreeSitterAnalysis,
    ) -> None:
        self.original_command = command
        self._command_bytes = command.encode("utf-8")
        self._pipe_positions = pipe_positions
        self._redirection_nodes = redirection_nodes
        self._tree_sitter_analysis = tree_sitter_analysis

    def to_string(self) -> str:
        return self.original_command

    def get_pipe_segments(self) -> list[str]:
        if len(self._pipe_positions) == 0:
            return [self.original_command]

        segments: list[str] = []
        current_start = 0

        for pipe_pos in self._pipe_positions:
            segment = self._command_bytes[current_start:pipe_pos].decode("utf-8").strip()
            if segment:
                segments.append(segment)
            current_start = pipe_pos + 1

        last_segment = self._command_bytes[current_start:].decode("utf-8").strip()
        if last_segment:
            segments.append(last_segment)

        return segments

    def without_output_redirections(self) -> str:
        if len(self._redirection_nodes) == 0:
            return self.original_command

        ordered = sorted(
            self._redirection_nodes, key=lambda r: r["startIndex"], reverse=True
        )

        result = self._command_bytes
        for redir in ordered:
            result = result[: redir["startIndex"]] + result[redir["endIndex"] :]
        return _WHITESPACE_RUN_RE.sub(" ", result.decode("utf-8").strip())

    def get_output_redirections(self) -> list[OutputRedirection]:
        return [
            {"target": r["target"], "operator": r["operator"]}
            for r in self._redirection_nodes
        ]

    def get_tree_sitter_analysis(self) -> TreeSitterAnalysis:
        return self._tree_sitter_analysis


# lodash memoize over a no-arg async fn → single-shot cache of the resolved boolean.
_tree_sitter_available: bool | None = None


async def _get_tree_sitter_available() -> bool:
    global _tree_sitter_available
    if _tree_sitter_available is not None:
        return _tree_sitter_available
    try:
        from tabvis.utils.bash.parser import parse_command

        test_result = await parse_command("echo test")
        _tree_sitter_available = test_result is not None
    except Exception:  # noqa: BLE001 — faithful to the TS bare catch
        _tree_sitter_available = False
    return _tree_sitter_available


def build_parsed_command_from_root(command: str, root: Node) -> IParsedCommand:
    """Build a :class:`TreeSitterParsedCommand` from a pre-parsed AST root.

    Lets callers that already have the tree skip the redundant ``native.parse`` that
    ``ParsedCommand.parse`` would do.
    """
    pipe_positions = _extract_pipe_positions(root)
    redirection_nodes = _extract_redirection_nodes(root)
    analysis = analyze_command(root, command)
    return TreeSitterParsedCommand(
        command, pipe_positions, redirection_nodes, analysis
    )


async def _do_parse(command: str) -> IParsedCommand | None:
    if not command:
        return None

    tree_sitter_available = await _get_tree_sitter_available()
    if tree_sitter_available:
        try:
            from tabvis.utils.bash.parser import parse_command

            data = await parse_command(command)
            if data:
                # Native NAPI parser returns plain dicts — nothing to free.
                return build_parsed_command_from_root(command, data["rootNode"])
        except Exception:  # noqa: BLE001 — fall through to the regex implementation
            pass

    # Fallback to regex implementation.
    return RegexParsedCommand_DEPRECATED(command)


# Single-entry cache: legacy callers may call ParsedCommand.parse repeatedly with the same command
# string. Caching the most recent command skips the redundant work; size-1 bound avoids leaking
# TreeSitterParsedCommand instances.
_last_cmd: str | None = None
_last_result: Any = None  # an awaitable (coroutine) of IParsedCommand | None


class _ParsedCommand:
    """``ParsedCommand`` namespace object (TS object literal with a ``parse`` method)."""

    def parse(self, command: str) -> Any:
        """Parse a command string → an awaitable of :class:`IParsedCommand` | ``None``.

        Returns ``None`` (awaited) if parsing fails completely. Uses tree-sitter when available
        (quote-aware), falling back to regex-based parsing otherwise.

        NOTE: the TS caches the *Promise* so repeated awaits share one parse. Python coroutines
        can only be awaited once, so each ``parse`` call on the same command returns a *fresh*
        coroutine while still short-circuiting the cache bookkeeping. Callers should ``await`` the
        returned coroutine exactly once (as they would the TS Promise).
        """
        global _last_cmd, _last_result
        if command == _last_cmd and _last_result is not None:
            # Re-derive a fresh coroutine for the cached command (single-await safety in Python).
            return _do_parse(command)
        _last_cmd = command
        _last_result = _do_parse(command)
        return _last_result


ParsedCommand = _ParsedCommand()


__all__ = [
    "IParsedCommand",
    "OutputRedirection",
    "ParsedCommand",
    "RegexParsedCommand_DEPRECATED",
    "TreeSitterParsedCommand",
    "build_parsed_command_from_root",
]
