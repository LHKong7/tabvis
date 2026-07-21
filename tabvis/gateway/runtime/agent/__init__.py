"""Agent Runtime — execute a Run's model/tool loop (design §7.8, §14 `runtime/agent/runner.py`).

This is the `RunLauncher` the orchestrator delegates execution to: it wraps tabvis's existing headless
agent loop (`stream_agent`) so a gateway Run actually runs an agent — driving the Run through
`preparing → running → completed | failed`, emitting durable domain events, and honoring cooperative
cancel. The model loop itself is unchanged (design non-goal: don't replace the model/tool loop); the
launcher only adapts it to the Run aggregate.
"""

from __future__ import annotations

from tabvis.gateway.runtime.agent.runner import AgentRunLauncher

__all__ = ["AgentRunLauncher"]
