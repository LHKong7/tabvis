"""Main runtime orchestrator.

Covers the headless ``-p/--print`` dispatch (parse args -> init configs -> run the headless
runner) and the default headless permission gate. The admin subcommand tree (mcp/skill/auth/
install/update...) and interactive paths are not implemented here; the no-argument path prints
headless-only guidance instead.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from tabvis.tool import ToolUseContext, get_empty_tool_permission_context
from tabvis.utils.config_constants import BROWSER_ENGINES
from tabvis.utils.permissions.permissions import get_deny_rule_for_tool

HEADLESS_ONLY_GUIDANCE = (
    "tabvis: the interactive UI has been removed; this runtime is headless-only.\n"
    'Provide a prompt with -p/--print (e.g. tabvis -p "hello") or run an admin subcommand.'
)


async def default_can_use_tool(
    tool: Any,
    input: Any,
    context: ToolUseContext,
    assistant_message: dict[str, Any],
    tool_use_id: str,
    force_decision: Any | None = None,
) -> dict[str, Any]:
    """Headless permission gate (SPINE_CONTRACTS #3).

    Blanket deny rules strip the tool; otherwise defer to the tool's ``check_permissions``. An
    ``ask`` decision resolves to **deny** in non-interactive mode (there is no prompt). With no
    configured rules the result is ``allow``.
    """
    app_state = context.get_app_state() if context.get_app_state else None
    permission_context = (
        (app_state or {}).get("toolPermissionContext") or get_empty_tool_permission_context()
    )
    if get_deny_rule_for_tool(permission_context, tool):
        return {
            "behavior": "deny",
            "message": f"{tool.name} is denied by a permission rule.",
            "decisionReason": {"type": "rule"},
        }
    decision = await tool.check_permissions(input, context)
    behavior = decision.get("behavior")
    if behavior == "ask":
        return {
            "behavior": "deny",
            "message": decision.get("message", "Permission required but cannot prompt (headless)."),
            "decisionReason": {"type": "mode", "mode": permission_context.get("mode", "default")},
        }
    if behavior == "passthrough":
        # No tool-level opinion; defer to the rule engine. There are no configured rules here
        # (blanket deny already checked above), so the no-rule default is allow.
        return {"behavior": "allow", "updatedInput": decision.get("updatedInput", input)}
    return decision


def _parse_args(args: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "prompt": None,
        "model": None,
        "output_format": "text",
        "max_turns": None,
        "browser_engine": None,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-p", "--print"):
            parsed["prompt"] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
            continue
        if a.startswith("--print="):
            parsed["prompt"] = a[len("--print=") :]
            i += 1
            continue
        if a == "--model" and i + 1 < len(args):
            parsed["model"] = args[i + 1]
            i += 2
            continue
        # Pick the browser driver for this run: --browser-engine chromium|cloak (alias --browser).
        # It just seeds TABVIS_BROWSER_ENGINE, which every browser accessor already reads first, so the
        # choice flows through launch, the console readiness check and the engine-mismatch guard with
        # no special-casing. A CLI value is validated in main() and fails loudly on a typo.
        if a in ("--browser-engine", "--browser") and i + 1 < len(args):
            parsed["browser_engine"] = args[i + 1]
            i += 2
            continue
        if a.startswith("--browser-engine="):
            parsed["browser_engine"] = a[len("--browser-engine=") :]
            i += 1
            continue
        if a.startswith("--browser="):
            parsed["browser_engine"] = a[len("--browser=") :]
            i += 1
            continue
        if a == "--output-format" and i + 1 < len(args):
            parsed["output_format"] = args[i + 1]
            i += 2
            continue
        if a == "--max-turns" and i + 1 < len(args):
            try:
                parsed["max_turns"] = int(args[i + 1])
            except ValueError:
                parsed["max_turns"] = None
            i += 2
            continue
        i += 1
    return parsed


async def main() -> None:
    args = sys.argv[1:]
    parsed = _parse_args(args)

    if parsed["prompt"] is None:
        # No -p/--print and no admin subcommand given: print headless-only guidance.
        print(HEADLESS_ONLY_GUIDANCE, file=sys.stderr)
        sys.exit(1)

    engine = parsed["browser_engine"]
    if engine is not None:
        if engine not in BROWSER_ENGINES:
            # A CLI flag is an explicit choice — reject a typo loudly instead of silently running
            # the default. Silently downgrading a requested 'cloak' to 'chromium' is exactly the
            # foot-gun the engine was built to avoid (a detectable browser where stealth was asked
            # for), so it must never happen by accident.
            print(
                f"tabvis: unknown --browser-engine {engine!r}; "
                f"choose one of: {', '.join(BROWSER_ENGINES)}.",
                file=sys.stderr,
            )
            sys.exit(2)
        # Seed the env var the browser accessors read first, so this wins over settings.json for the
        # whole run. Set before enable_configs()/the browser warm-up so nothing launches first.
        os.environ["TABVIS_BROWSER_ENGINE"] = engine

    from tabvis.utils.config import enable_configs

    enable_configs()

    from tabvis.ui.cli.print import run_headless

    await run_headless(
        parsed["prompt"],
        model=parsed["model"],
        output_format=parsed["output_format"],
        max_turns=parsed["max_turns"],
    )
