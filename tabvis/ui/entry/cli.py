"""Lightweight CLI router

Handles cheap fast paths (default server, ``--version``, ``--serve``, ``--dump-system-prompt``,
``--bare``) before loading the one-shot application in ``tabvis.agent.main``.
Imports are deferred to keep the fast paths cheap, mirroring the TS dynamic imports.
"""

from __future__ import annotations

import os
import sys

from tabvis.bootstrap_macro import MACRO

HELP_TEXT = """\
Usage:
  tabvis
  tabvis -p <prompt> [options]
  tabvis --serve [--host <host>] [--port <port>] [--dev]
  tabvis --dump-system-prompt [--model <model>]
  tabvis --version

Options:
  (no arguments)                     Start the Web console and local service.
  -p, --print <prompt>              Run one headless agent task.
  --model <model>                   Override the configured model.
  --output-format <format>          text, json, or stream-json.
  --max-turns <count>               Limit model turns (unbounded by default).
  --browser-engine, --browser <id>  Select the browser engine.
  --bare                            Use only Bash, Read, and Edit tools.
  --resume-plus <session-id>        Resume a saved session with context.
  --conversation-only              Resume conversation without agent memory.
  --no-memory                       Resume without reading or writing memory.
  --allow-new-browser               Allow a replacement browser when resuming.
  --serve                           Start the Web console and local HTTP/SSE service.
  --host <host>                     Service bind host.
  --port <port>                     Service bind port.
  --dev                             Use the live Vite console with HMR.
  --dump-system-prompt              Print the rendered system prompt.
  -v, -V, --version                 Print the Tabvis version.
  -h, --help                        Show this help.
"""


async def main() -> None:
    args = sys.argv[1:]

    if len(args) == 1 and args[0] in ("--help", "-h"):
        print(HELP_TEXT, end="")
        return

    # Fast-path for --version/-v: zero module loading needed.
    if len(args) == 1 and args[0] in ("--version", "-v", "-V"):
        # MACRO.VERSION is resolved from package metadata.
        print(f"{MACRO.VERSION} (Tabvis)")
        return

    from tabvis.utils.startup_profiler import profile_checkpoint

    profile_checkpoint("cli_entry")

    # Fast-path for direct startup or --serve: run the Web console + agent service.
    #   tabvis --serve [--host H] [--port N] [--dev]
    #   tabvis
    #   --dev replaces the bundled production console with live Vite/HMR.
    if not args or args[0] == "--serve":
        from tabvis.utils.config import enable_configs

        enable_configs()

        def _flag(name: str) -> str | None:
            if name in args:
                idx = args.index(name)
                if idx + 1 < len(args):
                    return args[idx + 1]
            return None

        # await on the loop we're already on — uvicorn.run() would try to open a second one.
        from tabvis.browser.server import serve_async

        from tabvis.utils.env_utils import is_env_truthy

        port_raw = _flag("--port")
        # --dev (or TABVIS_WEB_DEV=1): replace the built console with Vite/HMR.
        dev = "--dev" in args or is_env_truthy(os.environ.get("TABVIS_WEB_DEV"))
        await serve_async(host=_flag("--host"), port=int(port_raw) if port_raw else None, dev=dev)
        return

    # Fast-path for --dump-system-prompt: output the rendered system prompt and exit.
    if args and args[0] == "--dump-system-prompt":
        profile_checkpoint("cli_dump_system_prompt_path")
        from tabvis.utils.config import enable_configs

        enable_configs()
        from tabvis.utils.model.model import get_main_loop_model

        model = None
        if "--model" in args:
            idx = args.index("--model")
            if idx + 1 < len(args):
                model = args[idx + 1]
        model = model or get_main_loop_model()
        from tabvis.constants.prompts import get_system_prompt

        prompt = await get_system_prompt([], model)
        print("\n".join(prompt))
        return

    # --bare: set SIMPLE early so gates fire during option building.
    if "--bare" in args:
        os.environ["TABVIS_SIMPLE"] = "1"

    # One-shot flags: load and run the full headless CLI.
    profile_checkpoint("cli_before_main_import")
    from tabvis.agent.main import main as cli_main

    profile_checkpoint("cli_after_main_import")
    await cli_main()
    profile_checkpoint("cli_after_main_complete")
