"""First entrypoint.

Thin shim: load any ``.env`` file, install build-time macros, then hand off to the lightweight CLI
router. This module's :func:`main` is the console-script target (``tabvis``) and the
``python -m tabvis`` target.
"""

from __future__ import annotations

import asyncio

from tabvis.bootstrap_macro import ensure_bootstrap_macro


def main() -> None:
    # Load .env into os.environ BEFORE anything reads TABVIS_* configuration. Real exported vars win;
    # disable with TABVIS_DISABLE_DOTENV=1 (see tabvis.utils.dotenv_loader).
    from tabvis.utils.dotenv_loader import load_env_files

    load_env_files()

    ensure_bootstrap_macro()
    from tabvis.ui.entry import cli

    asyncio.run(cli.main())


if __name__ == "__main__":
    main()
