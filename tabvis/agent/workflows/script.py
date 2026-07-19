"""Workflow script loading and compilation.

Validates, transforms, and compiles a user-authored workflow script. The pure string/regex
helpers — :func:`validate_workflow_script`, :func:`transform_workflow_script`,
:func:`normalize_workflow_meta`, :func:`slugify_workflow_name` — are simple, self-contained
transforms.

:func:`evaluate_workflow_script` compiles a workflow script into its ``meta`` + ``workflow`` exports.
tabvis is a Python runtime with **no embedded JS engine**, so workflow scripts are Python and are
compiled via :mod:`tabvis.agent.workflows.engine`. Validation/transform/normalize/slugify are still
fully usable on their own (e.g. for the workflow command registry), and the module imports cleanly.

Casing: Python identifiers are snake_case; the returned ``WorkflowMeta`` keeps its wire keys
(``name``/``description``).
"""

from __future__ import annotations

import re
from typing import Any

from tabvis.utils.errors import get_error_message
from tabvis.agent.workflows.types import WorkflowMeta

# (pattern, message) pairs — a script matching any pattern is rejected. ``re.MULTILINE`` lets
# ``\b`` anchors work across lines.
BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bimport\b\s*(?:\(|[{\"*A-Za-z])", re.MULTILINE),
        "import is not available in workflow scripts",
    ),
    (
        re.compile(r"\brequire\s*\(", re.MULTILINE),
        "require is not available in workflow scripts",
    ),
    (
        re.compile(r"\bprocess\b", re.MULTILINE),
        "process is not available in workflow scripts",
    ),
    (re.compile(r"\bBun\b", re.MULTILINE), "Bun is not available in workflow scripts"),
    (
        re.compile(r"\beval\s*\(", re.MULTILINE),
        "eval is not available in workflow scripts",
    ),
    (
        re.compile(r"\bFunction\s*\(", re.MULTILINE),
        "Function is not available in workflow scripts",
    ),
    (
        re.compile(r"\bfs\b|\bnode:fs\b|\bchild_process\b|\bspawn\s*\(", re.MULTILINE),
        "direct filesystem and shell access are not available in workflow scripts",
    ),
]


def validate_workflow_script(script: str) -> None:
    """Raises ``ValueError`` on empty/blocked scripts."""
    if not script.strip():
        raise ValueError("Workflow script is empty")
    for pattern, message in BLOCKED_PATTERNS:
        if pattern.search(script):
            raise ValueError(message)


def transform_workflow_script(script: str) -> str:
    """Strip the ESM ``export`` wrappers and append the ``({ meta, workflow })`` epilogue.

    Raises ``ValueError`` if no ``workflow`` function is found.
    """
    transformed = script
    transformed = re.sub(r"export\s+const\s+meta\s*=", "const meta =", transformed)
    transformed = re.sub(
        r"export\s+default\s+async\s+function\s+workflow\s*\(",
        "async function workflow(",
        transformed,
    )
    transformed = re.sub(
        r"export\s+default\s+async\s+function\s*\(",
        "async function workflow(",
        transformed,
    )
    transformed = re.sub(
        r"export\s+default\s+function\s+workflow\s*\(",
        "function workflow(",
        transformed,
    )
    transformed = re.sub(
        r"export\s+default\s+function\s*\(", "function workflow(", transformed
    )
    transformed = re.sub(r"export\s+default\s+workflow\s*;?", "", transformed)

    if not re.search(r"\bfunction\s+workflow\s*\(", transformed):
        raise ValueError("Workflow script must export a default workflow function")

    transformed += '\n;({ meta: typeof meta === "undefined" ? undefined : meta, workflow });'
    return transformed


def evaluate_workflow_script(script: str) -> dict[str, Any]:
    """Validate + compile a workflow script into its ``{meta, workflow}`` exports.

    tabvis is a Python runtime with **no embedded JS engine**, so — per PRD OQ-1, which left the
    script language open — **workflow scripts are Python**. They map 1:1 onto the orchestration
    API the runner exposes (``agent`` / ``parallel`` / ``phase`` / ``log`` / ``args``) and are
    compiled + sandbox-executed by :mod:`tabvis.agent.workflows.engine`.

    This delegates to :func:`tabvis.agent.workflows.engine.compile_workflow`, which returns a
    ``{"meta": WorkflowMeta, "workflow": async (api) -> Any}`` dict that
    :func:`tabvis.agent.workflows.run.run_workflow` consumes unchanged. The
    ``Unable to load workflow script: ...`` wrapper is raised for invalid/unsafe scripts.

    The legacy ESM-syntax helpers (:func:`validate_workflow_script`, :func:`transform_workflow_script`)
    are retained for their standalone string-transform utility but are no longer on the execution
    path — :func:`tabvis.agent.workflows.engine.validate_python_workflow` is the live (Python-aware)
    validator.
    """
    from tabvis.agent.workflows.engine import WorkflowScriptError, compile_workflow

    try:
        return compile_workflow(script)
    except WorkflowScriptError as error:
        raise ValueError(
            f"Unable to load workflow script: {get_error_message(error)}"
        ) from error


def normalize_workflow_meta(
    meta: Any, fallback_name: str = "dynamic-workflow"
) -> WorkflowMeta:
    """Coerce arbitrary input into a ``WorkflowMeta`` dict."""
    if not meta or not isinstance(meta, dict):
        return {"name": fallback_name}
    raw_name = meta.get("name")
    raw_name = raw_name.strip() if isinstance(raw_name, str) else ""
    raw_description = meta.get("description")
    raw_description = raw_description.strip() if isinstance(raw_description, str) else ""
    result: WorkflowMeta = {"name": raw_name or fallback_name}
    if raw_description:
        result["description"] = raw_description
    return result


def slugify_workflow_name(name: str) -> str:
    """Lowercase, non-alnum→``-``, trim leading/trailing ``-``."""
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"^-+|-+$", "", slug)
    return slug or "dynamic-workflow"
