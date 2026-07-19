"""Dynamic-workflow script **execution engine**.

The TS runtime evaluated a JavaScript workflow script inside a ``node:vm`` sandbox. tabvis is a Python
runtime with no embedded JS engine, so — per PRD OQ-1 (script language left open) — **workflow
scripts are Python**. This maps 1:1 onto the orchestration API the runner already exposes
(``runAgent`` → ``agent``, ``Promise.all`` → ``parallel``/``gather``, the ``args`` global,
``phase``/``log``) and runs natively in tabvis's asyncio scheduler.

A workflow script is a top-level Python program that:
  * declares a literal ``meta = {"name": ..., "description": ..., "phases": [...]}`` (read WITHOUT
    executing the body, for the approval card),
  * runs orchestration code with ``agent`` / ``parallel`` / ``gather`` / ``pipeline`` / ``phase`` /
    ``log`` / ``args`` in scope (each ``agent(...)`` spawns a subagent; intermediate results stay in
    script variables — only the script's ``return`` value flows back to the main session),
  * ``return``s the final result.

Example::

    meta = {"name": "review-routes", "description": "Audit each route for missing auth",
            "phases": [{"title": "scan"}, {"title": "review"}]}

    phase("scan")
    findings = await parallel([(lambda f=f: agent({"prompt": f"Review {f}", "phase": "scan"}))
                               for f in args])
    return {"findings": [f["result"] for f in findings]}

**Security (PRD §9 / S-1):** the script has NO direct filesystem or shell access — all side effects
go through ``agent`` (a sandboxed subagent). Enforcement is two-layer: (1) an AST validator that
rejects ``import``, ``open``/``exec``/``eval``/``__import__``, the dangerous builtins, and dunder
attribute traversal (the classic ``().__class__.__bases__`` sandbox-escape); (2) a restricted exec
namespace exposing only a curated safe-builtins set plus the orchestration API. Workflow scripts are
**model-generated and user-approved** (not adversarial input), so this is a defense-in-depth bar, not
a hostile-code sandbox — the real isolation is at the subagent level.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Awaitable, Callable
from typing import Any

from tabvis.agent.workflows.types import WorkflowMeta

__all__ = [
    "WorkflowScriptError",
    "compile_workflow",
    "extract_workflow_meta",
    "validate_python_workflow",
]


class WorkflowScriptError(ValueError):
    """Raised when a workflow script is invalid or unsafe."""


# Names a workflow script may never reference (FS/shell/escape vectors). ``agent`` is the only way to
# touch the filesystem or run commands.
_FORBIDDEN_NAMES = frozenset(
    {
        "open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
        "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
        "exit", "quit", "help", "copyright", "credits", "license",
    }
)

# Attribute names that enable sandbox escape via object traversal.
_FORBIDDEN_ATTRS = frozenset(
    {
        "__class__", "__bases__", "__base__", "__mro__", "__subclasses__", "__globals__",
        "__dict__", "__builtins__", "__code__", "__closure__", "__func__", "__self__",
        "__module__", "__getattribute__", "__reduce__", "__reduce_ex__", "__init_subclass__",
        "__subclasshook__", "__loader__", "__spec__", "__import__", "f_globals", "f_locals",
        "f_builtins", "gi_frame", "cr_frame",
    }
)

# A curated, side-effect-free builtins set the orchestration code may use.
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "hash", "hex", "int", "isinstance", "issubclass", "iter", "len", "list", "map",
    "max", "min", "next", "oct", "ord", "chr", "pow", "range", "repr", "reversed", "round", "set",
    "slice", "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
    "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError", "StopIteration",
    "StopAsyncIteration", "Exception",
)


def _safe_builtins() -> dict[str, Any]:
    import builtins

    out: dict[str, Any] = {}
    for name in _SAFE_BUILTIN_NAMES:
        if hasattr(builtins, name):
            out[name] = getattr(builtins, name)
    return out


def validate_python_workflow(script: str) -> ast.Module:
    """Parse + statically validate a Python workflow script. Returns the AST on success.

    Raises :class:`WorkflowScriptError` for syntax errors, ``import`` statements, forbidden
    builtins, or dunder attribute traversal (sandbox-escape vectors).
    """
    if not script.strip():
        raise WorkflowScriptError("Workflow script is empty")
    try:
        tree = ast.parse(script, mode="exec")
    except SyntaxError as exc:
        raise WorkflowScriptError(f"Workflow script syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise WorkflowScriptError(
                "import is not allowed in workflow scripts — orchestration only; "
                "all side effects must go through agent()"
            )
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise WorkflowScriptError(
                f"'{node.id}' is not available in workflow scripts (no filesystem/shell/eval access)"
            )
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRS:
            raise WorkflowScriptError(
                f"attribute access to '{node.attr}' is not allowed in workflow scripts"
            )
        # `from x import *`-style or attribute-call to __import__ etc. are covered above.
    return tree


def extract_workflow_meta(script: str, fallback_name: str = "workflow") -> WorkflowMeta:
    """Read the literal ``meta = {...}`` assignment WITHOUT executing the script body.

    Used for the approval card / saved-command registration. The ``meta`` value must be a pure
    literal (no variables/calls) — matching the TS ``meta`` contract.
    """
    try:
        tree = ast.parse(script, mode="exec")
    except SyntaxError:
        return {"name": fallback_name, "description": ""}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "meta":
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        return {"name": fallback_name, "description": ""}
                    if isinstance(value, dict):
                        meta: WorkflowMeta = {
                            "name": str(value.get("name") or fallback_name),
                            "description": str(value.get("description") or ""),
                        }
                        if "phases" in value:
                            meta["phases"] = value["phases"]  # type: ignore[typeddict-unknown-key]
                        return meta
    return {"name": fallback_name, "description": ""}


def compile_workflow(script: str, *, source_path: str = "<workflow>") -> dict[str, Any]:
    """Validate + compile a Python workflow script.

    Returns ``{"meta": WorkflowMeta, "workflow": async (api) -> Any}``. The returned ``workflow``
    coroutine function, when ``await``-ed with the runner's ``api`` dict (``agent`` / ``parallel`` /
    ``phase`` / ``log`` / ``args`` / ``meta``), executes the script body in the restricted sandbox
    and returns the script's ``return`` value. Mirrors the shape the TS ``evaluateWorkflowScript``
    produced, so :func:`tabvis.agent.workflows.run.run_workflow` consumes it unchanged.
    """
    validate_python_workflow(script)
    meta = extract_workflow_meta(script)

    # Wrap the user body in an async function so top-level ``await`` / ``return`` are legal.
    wrapped_src = "async def __tabvis_workflow__():\n" + textwrap.indent(script, "    ")
    try:
        code = compile(wrapped_src, source_path, "exec")
    except SyntaxError as exc:
        raise WorkflowScriptError(f"Workflow script syntax error: {exc}") from exc

    async def workflow(api: dict[str, Any]) -> Any:
        import asyncio

        namespace: dict[str, Any] = {
            "__builtins__": _safe_builtins(),
            # Orchestration API (provided by the runner) — the script's globals.
            "agent": _wrap_agent(api.get("agent")),
            "parallel": api.get("parallel"),
            "phase": _wrap_phase(api.get("phase")),
            "log": api.get("log") or (lambda *_a, **_k: None),
            "args": api.get("args"),
            "meta": api.get("meta", meta),
            "budget": api.get("budget"),
            # Convenience aliases the PRD / common patterns use.
            "gather": asyncio.gather,
            "pipeline": _make_pipeline(api.get("parallel")),
            "workflow": api.get("workflow"),
        }
        exec(code, namespace)  # noqa: S102 - restricted namespace; model-generated approved script
        return await namespace["__tabvis_workflow__"]()

    return {"meta": meta, "workflow": workflow}


class _AttrDict(dict):
    """A dict that also allows attribute access (``r.result`` ≡ ``r["result"]``).

    Workflow scripts are model-generated, and LLMs frequently write ``r.result`` / ``r.name``
    instead of the documented subscript form. Returning the ``agent()`` result as an ``_AttrDict``
    accepts both so a single idiom slip doesn't abort an otherwise-correct workflow.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # surface as AttributeError so normal attr semantics hold
            raise AttributeError(name) from exc


def _as_attr_dict(value: Any) -> Any:
    """Wrap a plain ``dict`` result as an :class:`_AttrDict` (leaves non-dicts untouched)."""
    if isinstance(value, dict) and not isinstance(value, _AttrDict):
        return _AttrDict(value)
    return value


def _wrap_agent(raw_agent: Any) -> Any:
    """Adapt the runner's ``agent(input_dict)`` to also accept a bare prompt string + opts.

    Lets a workflow script call ``await agent("do X")``, ``await agent("do X", {"name": "n"})``, or
    the canonical ``await agent({"prompt": "do X", "name": "n"})`` interchangeably. The result is an
    :class:`_AttrDict` so scripts can use either ``r["result"]`` or ``r.result``.
    """
    if raw_agent is None:
        return None

    async def agent(spec: Any, opts: Any = None) -> Any:
        if isinstance(spec, str):
            inp: dict[str, Any] = {"prompt": spec}
            if isinstance(opts, dict):
                inp.update(opts)
        else:
            inp = spec
        return _as_attr_dict(await raw_agent(inp))

    return agent


class _CompletedAwaitable:
    """A no-op, already-resolved awaitable.

    Lets ``phase(name)`` be called with or without ``await``: the runner's ``phase`` does its
    synchronous work eagerly when called, and this object stands in as the return value so both
    ``phase("scan")`` (discarded harmlessly — it is a plain object, not a coroutine, so no
    "never awaited" warning) and ``await phase("scan")`` (resolves to ``value``) are valid.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any = None) -> None:
        self._value = value

    def __await__(self) -> Any:
        return self._resolve()

    def _resolve(self) -> Any:
        return self._value
        yield  # pragma: no cover - unreachable; marks this as a generator (a valid awaitable)


def _wrap_phase(raw_phase: Any) -> Any:
    """Make ``phase(name)`` usable whether or not it is ``await``-ed.

    The runner's ``phase`` records the phase synchronously (output + task state, no awaits). We run
    it eagerly so the phase is registered the moment the script calls ``phase(...)`` — even when the
    next ``await`` does not actually suspend the loop — and return a :class:`_CompletedAwaitable` so a
    forgotten (or deliberate) ``await`` degrades gracefully instead of raising.
    """
    if raw_phase is None:
        return lambda *_a, **_k: _CompletedAwaitable()

    def phase(name: Any) -> Any:
        import asyncio

        result = raw_phase(name)
        if asyncio.iscoroutine(result):
            # Defensive: if a runner ever hands back a coroutine, schedule it so it still runs.
            return asyncio.ensure_future(result)
        return _CompletedAwaitable(result)

    return phase


def _make_pipeline(parallel: Any) -> Callable[..., Awaitable[list[Any]]]:
    """Build a ``pipeline(items, *stages)`` helper from the runner's ``parallel``.

    Each item flows through every stage independently (no barrier between stages): item A can be in
    stage 3 while item B is still in stage 1. A stage that throws drops that item to ``None``.
    """

    async def pipeline(items: list[Any], *stages: Callable[..., Any]) -> list[Any]:
        import asyncio

        async def run_item(item: Any, index: int) -> Any:
            value: Any = item
            for stage in stages:
                try:
                    produced = stage(value, item, index)
                    value = await produced if asyncio.iscoroutine(produced) else produced
                except Exception:  # noqa: BLE001 - drop the item, keep the pipeline going
                    return None
            return value

        if parallel is None:
            return await asyncio.gather(*(run_item(it, i) for i, it in enumerate(items)))
        # Reuse the runner's concurrency-capped scheduler.
        return await parallel([(lambda it=it, i=i: run_item(it, i)) for i, it in enumerate(items)])

    return pipeline
