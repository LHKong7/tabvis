"""Lightweight CLI router

Handles cheap fast paths (``--version``, ``--serve``, ``--dump-system-prompt``, ``--bare``) before
loading the full application in ``tabvis.agent.main``.
Imports are deferred to keep the fast paths cheap, mirroring the TS dynamic imports.
"""

from __future__ import annotations

import os
import sys

from tabvis.bootstrap_macro import MACRO


async def main() -> None:
    args = sys.argv[1:]

    # Fast-path for --version/-v: zero module loading needed.
    if len(args) == 1 and args[0] in ("--version", "-v", "-V"):
        # MACRO.VERSION is resolved from package metadata.
        print(f"{MACRO.VERSION} (Tabvis)")
        return

    from tabvis.utils.startup_profiler import profile_checkpoint

    profile_checkpoint("cli_entry")

    # Fast-path for --serve: run the HTTP/SSE agent server instead of a one-shot turn.
    #   tabvis --serve [--host H] [--port N] [--dev]
    #   --dev serves the console live from web/ via Vite (HMR); default serves the built bundle.
    if args and args[0] == "--serve":
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
        # --dev (or TABVIS_WEB_DEV=1): serve the console live from web/ via Vite (HMR), not the build.
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

    # No special flags: load and run the full headless CLI.
    profile_checkpoint("cli_before_main_import")
    from tabvis.agent.main import main as cli_main

    profile_checkpoint("cli_after_main_import")
    await cli_main()
    profile_checkpoint("cli_after_main_complete")
