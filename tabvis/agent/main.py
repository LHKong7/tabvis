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
        # Resume Plus (design §12.1). ``resume_plus`` holds the selected session id when requested.
        "resume_plus": None,
        "conversation_only": False,
        "no_memory": False,
        "allow_new_browser": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-p", "--print"):
            parsed["prompt"] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
            continue
        # --- Resume Plus flags ---
        if a in ("--resume-plus", "--resume") and i + 1 < len(args):
            parsed["resume_plus"] = args[i + 1]
            i += 2
            continue
        if a.startswith("--resume-plus="):
            parsed["resume_plus"] = a[len("--resume-plus=") :]
            i += 1
            continue
        if a.startswith("--resume="):
            parsed["resume_plus"] = a[len("--resume=") :]
            i += 1
            continue
        if a == "--conversation-only":
            parsed["conversation_only"] = True
            i += 1
            continue
        if a == "--no-memory":
            parsed["no_memory"] = True
            i += 1
            continue
        if a == "--allow-new-browser":
            parsed["allow_new_browser"] = True
            i += 1
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

    # Resume-flag validation: reject dependent/conflicting combinations rather than ignoring them.
    if parsed["resume_plus"] is None:
        if parsed["conversation_only"] or parsed["no_memory"] or parsed["allow_new_browser"]:
            print(
                "tabvis: --conversation-only / --no-memory / --allow-new-browser require "
                "--resume-plus <session_id>.",
                file=sys.stderr,
            )
            sys.exit(2)
    elif not parsed["resume_plus"].strip():
        print("tabvis: --resume-plus requires a session id.", file=sys.stderr)
        sys.exit(2)
    elif parsed["conversation_only"] and parsed["no_memory"]:
        # Both mean "no agent memory this run"; accepting both silently hides a likely mistake.
        print(
            "tabvis: --conversation-only already excludes Agent Memory; drop --no-memory.",
            file=sys.stderr,
        )
        sys.exit(2)

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

    resume_target = None
    if parsed["resume_plus"] is not None:
        resume_target = _resolve_cli_resume(parsed)

    await run_headless(
        parsed["prompt"],
        model=parsed["model"],
        output_format=parsed["output_format"],
        max_turns=parsed["max_turns"],
        resume_target=resume_target,
    )


def _resolve_cli_resume(parsed: dict[str, Any]) -> Any:
    """Resolve the CLI's Resume selector to a ``ResumeTarget``, exiting cleanly on a Resume error."""
    from tabvis.agent.resume_plus import ResumeError, resolve_resume

    mode = "conversation_only" if parsed["conversation_only"] else "plus"
    read_write = not (parsed["no_memory"] or parsed["conversation_only"])
    try:
        return resolve_resume(
            parsed["resume_plus"].strip(),
            mode=mode,
            current_cwd=os.getcwd(),
            allow_new_browser=parsed["allow_new_browser"],
            read_memory=read_write,
            write_memory=read_write,
            resident=False,  # a one-shot CLI is never a resident daemon (§6.1)
        )
    except ResumeError as e:
        print(f"tabvis: cannot resume: {e.code}: {e.message}", file=sys.stderr)
        sys.exit(3)
