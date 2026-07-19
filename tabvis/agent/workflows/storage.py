"""Workflow on-disk storage.

Save generated workflow scripts to the personal (``~/.tabvis/workflows``) or project
(``.tabvis/workflows`` up to the git root) dirs, and load the saved ``.py`` files back into
:class:`tabvis.agent.workflows.types.WorkflowCommandSpec` entries for the command registry.

Workflow scripts are **Python** (see :mod:`tabvis.agent.workflows.engine`), so saved files use the
``.py`` extension. Directory listing and existence checks go through :mod:`pathlib` /
:func:`os.listdir`; saved files are written with ``mode: 0o600`` via :func:`os.chmod` after write;
the user/project merge is a ``dict`` keyed by slug (later entries override earlier — project
wins over user). Invalid/unsafe saved workflows are validated + skipped so command loading stays
robust. ``WorkflowMeta`` keeps its wire keys (``name`` / ``description``).
"""

from __future__ import annotations

import os
from pathlib import Path

from tabvis.utils.markdown_config_loader import get_project_dirs_up_to_home
from tabvis.agent.workflows.script import (
    evaluate_workflow_script,
    normalize_workflow_meta,
    slugify_workflow_name,
)
from tabvis.agent.workflows.types import (
    GeneratedWorkflow,
    WorkflowCommandSpec,
    WorkflowMeta,
)


def get_personal_workflow_dir() -> str:
    """``~/.tabvis/workflows``."""
    return str(Path.home() / ".tabvis" / "workflows")


async def save_workflow_to_dir(dir: str, workflow: GeneratedWorkflow) -> str:
    """Save a workflow script to ``dir`` under a collision-free ``<slug>[-N].py`` filename."""
    Path(dir).mkdir(parents=True, exist_ok=True)

    base_slug = slugify_workflow_name(workflow["meta"]["name"])
    for suffix in range(1000):
        filename = f"{base_slug}.py" if suffix == 0 else f"{base_slug}-{suffix + 1}.py"
        target = Path(dir) / filename
        if not target.exists():
            target.write_text(workflow["script"], encoding="utf-8")
            os.chmod(target, 0o600)
            return str(target)
    raise RuntimeError(
        f"Could not find an available filename for workflow {workflow['meta']['name']}"
    )


async def save_personal_workflow(workflow: GeneratedWorkflow) -> str:
    """Save a workflow to the personal workflow dir."""
    return await save_workflow_to_dir(get_personal_workflow_dir(), workflow)


def _load_workflow_specs_from_dir_sync(dir: str, source: str) -> list[WorkflowCommandSpec]:
    """Synchronous core of the per-dir spec loader (only sync disk I/O is involved)."""
    if not Path(dir).exists():
        return []
    specs: list[WorkflowCommandSpec] = []
    for name in os.listdir(dir):
        script_path = os.path.join(dir, name)
        if not os.path.isfile(script_path) or not name.endswith(".py"):
            continue
        try:
            script = Path(script_path).read_text(encoding="utf-8")
            fallback = name[: -len(".py")]
            evaluated = evaluate_workflow_script(script)
            meta: WorkflowMeta = normalize_workflow_meta(evaluated.get("meta"), fallback)
            specs.append(
                {
                    "name": slugify_workflow_name(meta["name"]),
                    "description": meta.get("description") or f"Run workflow {meta['name']}",
                    "scriptPath": script_path,
                    "source": source,
                    "meta": meta,
                }
            )
        except Exception:  # noqa: BLE001 — invalid saved workflows are ignored for robustness.
            continue
    return specs


def _merge_specs(
    user_specs: list[WorkflowCommandSpec], project_specs: list[WorkflowCommandSpec]
) -> list[WorkflowCommandSpec]:
    """Merge user + project specs (project overrides user on slug collision)."""
    by_name: dict[str, WorkflowCommandSpec] = {}
    for spec in user_specs:
        by_name[spec["name"]] = spec
    for spec in project_specs:
        by_name[spec["name"]] = spec
    return list(by_name.values())


def load_workflow_command_specs_sync(cwd: str) -> list[WorkflowCommandSpec]:
    """Synchronous spec loader for the (sync) command registry — personal + project dirs.

    The async variants below wrap this same disk-scanning logic in coroutines; this sync sibling
    is what :func:`tabvis.ui.commands.get_commands` (a synchronous aggregator) calls.
    """
    project_dirs = get_project_dirs_up_to_home("workflows", cwd)
    user_specs = _load_workflow_specs_from_dir_sync(get_personal_workflow_dir(), "user")
    project_specs: list[WorkflowCommandSpec] = []
    for d in project_dirs:
        project_specs.extend(_load_workflow_specs_from_dir_sync(os.path.realpath(d), "project"))
    return _merge_specs(user_specs, project_specs)


async def _load_workflow_specs_from_dir(dir: str, source: str) -> list[WorkflowCommandSpec]:
    return _load_workflow_specs_from_dir_sync(dir, source)


async def load_workflow_command_specs(cwd: str) -> list[WorkflowCommandSpec]:
    """Load workflow command specs from the personal + project (up to git root) dirs."""
    project_dirs = get_project_dirs_up_to_home("workflows", cwd)
    return await load_workflow_command_specs_from_dirs(get_personal_workflow_dir(), project_dirs)


async def load_workflow_command_specs_from_dirs(
    user_dir: str, project_dirs: list[str]
) -> list[WorkflowCommandSpec]:
    """Merge user + project workflow specs (project overrides user on slug collision)."""
    user_specs = await _load_workflow_specs_from_dir(user_dir, "user")
    project_specs: list[WorkflowCommandSpec] = []
    for d in project_dirs:
        project_specs.extend(await _load_workflow_specs_from_dir(os.path.realpath(d), "project"))
    return _merge_specs(user_specs, project_specs)
