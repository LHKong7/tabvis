"""CanUseToolFn type.

A permission gate invoked per tool call. Returns a :data:`PermissionDecision`.
Signature (for reference): ``(tool, input, tool_use_context, assistant_message,
tool_use_id, force_decision?) -> Awaitable[PermissionDecision]``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from tabvis.types.permissions import PermissionDecision

CanUseToolFn = Callable[..., Awaitable[PermissionDecision]]
