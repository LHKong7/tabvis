"""Plugin dependency graph (design §8.5).

Dependency graphs MUST be acyclic; plugins start in topological order and stop in reverse (design
§8.5). :func:`topological_order` returns a start order (a dependency before its dependents) and raises
on a cycle or a missing dependency, so an unstartable graph is caught before anything is started.
"""

from __future__ import annotations

from tabvis.gateway.protocol.errors import GatewayError


def topological_order(deps: dict[str, tuple[str, ...]]) -> list[str]:
    """Start order for plugins given ``{plugin_id: (dependency_ids,...)}``.

    Raises ``VALIDATION_FAILED`` on a cycle or a reference to an unknown plugin.
    """
    # Validate references first — a dependency on a plugin that isn't present is unsatisfiable.
    for plugin_id, requires in deps.items():
        for dep in requires:
            if dep not in deps:
                raise GatewayError(
                    "VALIDATION_FAILED",
                    message=f"Plugin {plugin_id!r} depends on unknown plugin {dep!r}",
                    details={"plugin": plugin_id, "missing": dep},
                )

    order: list[str] = []
    # states: 0 = unvisited, 1 = visiting (on stack), 2 = done
    state: dict[str, int] = {p: 0 for p in deps}

    def visit(node: str, stack: tuple[str, ...]) -> None:
        if state[node] == 2:
            return
        if state[node] == 1:
            cycle = " -> ".join([*stack, node])
            raise GatewayError("VALIDATION_FAILED", message=f"Dependency cycle: {cycle}", details={"cycle": cycle})
        state[node] = 1
        for dep in deps[node]:
            visit(dep, (*stack, node))
        state[node] = 2
        order.append(node)

    for plugin_id in sorted(deps):  # deterministic order
        visit(plugin_id, ())
    return order
