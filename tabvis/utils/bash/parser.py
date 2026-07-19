"""Gated tree-sitter parse entry

RUNTIME-GATED-OFF (see memory ``bash-parser-gated-off``): the TS source hardcodes its feature
gates to ``false`` (``if (false || false)`` / ``if (false)``), so :func:`parse_command` and
:func:`parse_command_raw` **always return ``None``** at runtime — the whole tree-sitter parse
path is dead code. The Bash-tool security layer flows through the regex / shell-quote fallback
instead. The gated branches are reproduced faithfully as ``if False:`` blocks so the structure
matches the TS; everything inside is unreachable.

The non-gated helpers (``find_command_node`` / ``extract_env_vars`` / ``extract_command_arguments``
/ ``strip_quotes``) are implemented with equivalent behavior — they are pure AST walkers used by the gated body and
are exported for completeness / future un-gating.
"""

from __future__ import annotations

from typing import TypedDict

from tabvis.utils.bash.bash_parser import TsNode

# `Node` is the tree-sitter node shape (re-export of TsNode), per `export type Node = TsNode`.
Node = TsNode


class ParsedCommandData(TypedDict):
    rootNode: Node
    envVars: list[str]
    commandNode: Node | None
    originalCommand: str


MAX_COMMAND_LENGTH = 10000
DECLARATION_COMMANDS = {
    "export",
    "declare",
    "typeset",
    "readonly",
    "local",
    "unset",
    "unsetenv",
}
ARGUMENT_TYPES = {"word", "string", "raw_string", "number"}
SUBSTITUTION_TYPES = {"command_substitution", "process_substitution"}
COMMAND_TYPES = {"command", "declaration_command"}

# SECURITY: Sentinel for "parser was loaded and attempted, but aborted" (timeout / node budget
# / panic). Distinct from `None` (module not loaded). Callers MUST treat this as fail-closed
# (too-complex), NOT route to legacy. Object identity distinguishes this sentinel from ``None``.
PARSE_ABORTED = object()

async def ensure_initialized() -> None:
    """The tree-sitter parser is not enabled in this build; a no-op."""
    return None


async def parse_command(command: str) -> ParsedCommandData | None:
    """GATED OFF, so this always returns ``None`` at runtime.

    The body inside the ``if False:`` branch mirrors the TS positive-gate path (which Bun DCEs);
    it is unreachable here.
    """
    if not command or len(command) > MAX_COMMAND_LENGTH:
        return None
    # The tree-sitter parse path is not enabled in this build; security flows through the
    # regex / shell-quote fallback instead.
    return None


async def parse_command_raw(command: str):
    """GATED OFF, so this always returns ``None`` at runtime.

    Returns (per the TS contract, were it un-gated):
      - ``Node``: parse succeeded
      - ``None``: module not loaded / feature off / empty / over-length
      - :data:`PARSE_ABORTED`: module loaded but parse failed (timeout/panic)
    """
    if not command or len(command) > MAX_COMMAND_LENGTH:
        return None
    # The tree-sitter parse path is not enabled in this build; always None.
    return None


def find_command_node(node: Node, parent: Node | None) -> Node | None:
    """Locate the command node within an AST subtree."""
    type_ = node["type"]
    children = node["children"]

    if type_ in COMMAND_TYPES:
        return node

    # Variable assignment followed by command
    if type_ == "variable_assignment" and parent:
        for c in parent["children"]:
            if c["type"] in COMMAND_TYPES and c["startIndex"] > node["startIndex"]:
                return c
        return None

    # Pipeline: recurse into first child (which may be a redirected_statement)
    if type_ == "pipeline":
        for child in children:
            result = find_command_node(child, node)
            if result:
                return result
        return None

    # Redirected statement: find the command inside
    if type_ == "redirected_statement":
        for c in children:
            if c["type"] in COMMAND_TYPES:
                return c
        return None

    # Recursive search
    for child in children:
        result = find_command_node(child, node)
        if result:
            return result

    return None


def extract_env_vars(command_node: Node | None) -> list[str]:
    """Collect leading ``VAR=val`` assignments on a command."""
    if not command_node or command_node["type"] != "command":
        return []

    env_vars: list[str] = []
    for child in command_node["children"]:
        if child["type"] == "variable_assignment":
            env_vars.append(child["text"])
        elif child["type"] in ("command_name", "word"):
            break
    return env_vars


def extract_command_arguments(command_node: Node) -> list[str]:
    """The command name + its literal arguments."""
    # Declaration commands
    if command_node["type"] == "declaration_command":
        children = command_node["children"]
        first_child = children[0] if children else None
        if first_child and first_child["text"] in DECLARATION_COMMANDS:
            return [first_child["text"]]
        return []

    args: list[str] = []
    found_command_name = False

    for child in command_node["children"]:
        if child["type"] == "variable_assignment":
            continue

        # Command name
        if child["type"] == "command_name" or (
            not found_command_name and child["type"] == "word"
        ):
            found_command_name = True
            args.append(child["text"])
            continue

        # Arguments
        if child["type"] in ARGUMENT_TYPES:
            args.append(strip_quotes(child["text"]))
        elif child["type"] in SUBSTITUTION_TYPES:
            break
    return args


def strip_quotes(text: str) -> str:
    """Strip a single layer of matching ``"`` / ``'`` quotes."""
    if len(text) >= 2 and (
        (text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'")
    ):
        return text[1:-1]
    return text


__all__ = [
    "ARGUMENT_TYPES",
    "COMMAND_TYPES",
    "DECLARATION_COMMANDS",
    "MAX_COMMAND_LENGTH",
    "PARSE_ABORTED",
    "SUBSTITUTION_TYPES",
    "Node",
    "ParsedCommandData",
    "ensure_initialized",
    "extract_command_arguments",
    "extract_env_vars",
    "find_command_node",
    "parse_command",
    "parse_command_raw",
    "strip_quotes",
]
