"""``/dynamic-workflow`` local command (metadata)

Generates, saves, and runs a dynamic workflow. :func:`load` lazy-loads
``dynamic_workflow_impl.call``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tabvis.types.command import LocalCommand

if TYPE_CHECKING:
    from tabvis.types.command import LocalCommandModule

__all__ = ["dynamic_workflow", "load"]


async def load() -> LocalCommandModule:
    """Lazy-load the implementation module."""
    from tabvis.ui.commands.dynamic_workflow import dynamic_workflow_impl

    return {"call": dynamic_workflow_impl.call}


dynamic_workflow = LocalCommand(
    type="local",
    name="dynamic-workflow",
    description="Generate, save, and run a dynamic workflow",
    supports_non_interactive=True,
    load=load,
)
