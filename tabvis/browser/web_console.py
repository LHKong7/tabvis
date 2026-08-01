"""Serve the bundled Tabvis React console.

Production builds live inside the Python package at ``tabvis/browser/static`` so wheels and source
checkouts expose the same UI. ``TABVIS_WEB_DIR`` remains a development/test escape hatch: when set,
production assets are read from ``<TABVIS_WEB_DIR>/dist``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# These first path segments belong to the HTTP API. Unknown endpoints under them must remain 404
# instead of receiving the SPA shell, which would hide disabled/misspelled API routes.
_API_ROOTS = frozenset(
    {"agent", "agents", "browser", "browsers", "config", "executions", "health", "v1", "workspaces"}
)


def static_dir() -> Path:
    """Return the directory containing the production console bundle."""
    override = os.environ.get("TABVIS_WEB_DIR")
    if override:
        return Path(override).expanduser() / "dist"
    return Path(__file__).with_name("static")


def _inside(path: Path, directory: Path) -> bool:
    """Whether a resolved path is contained by ``directory`` (including symlink targets)."""
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


async def serve_built_console(request: Any) -> Any:
    """Serve a built asset, falling back to ``index.html`` for client-side routes."""
    from starlette.responses import FileResponse, PlainTextResponse

    relative = request.path_params.get("path", "")
    if relative and relative.split("/", 1)[0] in _API_ROOTS:
        return PlainTextResponse("Not found", status_code=404)

    directory = static_dir().resolve()
    index = directory / "index.html"
    if not index.is_file():
        return PlainTextResponse(
            "Tabvis web console is not built. Run `cd web && npm install && npm run build`, "
            "then restart Tabvis.",
            status_code=503,
        )

    if relative:
        candidate = (directory / relative).resolve()
        if not _inside(candidate, directory):
            return PlainTextResponse("Not found", status_code=404)
        if candidate.is_file():
            return FileResponse(candidate)
        # Missing files must stay 404 (returning index.html for a JS/CSS request produces confusing
        # MIME and parse errors). Extensionless paths are React Router locations and use the shell.
        if Path(relative).suffix:
            return PlainTextResponse("Not found", status_code=404)

    response = FileResponse(index, media_type="text/html")
    response.headers["Cache-Control"] = "no-cache"
    return response
