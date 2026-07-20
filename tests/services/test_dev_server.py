"""--serve --dev wiring: web/ location, proxy target, and route table (prod path unchanged)."""

from __future__ import annotations

import os

from starlette.routing import Route

from tabvis.browser import dev_server
from tabvis.browser.server import create_app


def test_web_dir_points_at_repo_web() -> None:
    wd = dev_server.web_dir()
    assert os.path.basename(wd) == "web"
    # sibling of the tabvis package, i.e. <repo>/web and <repo>/tabvis both exist
    repo = os.path.dirname(wd)
    assert os.path.isdir(os.path.join(repo, "tabvis"))


def test_web_dir_env_override(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_WEB_DIR", "/tmp/somewhere/web")
    assert dev_server.web_dir() == "/tmp/somewhere/web"


def test_vite_base_from_env(monkeypatch) -> None:
    # VITE_BASE is computed at import; just assert the default composition is sane.
    assert dev_server.VITE_BASE == f"http://{dev_server.VITE_HOST}:{dev_server.VITE_PORT}"
    assert dev_server.VITE_BASE.startswith("http://")


def _paths(app) -> list[str]:
    return [r.path for r in app.routes if isinstance(r, Route)]


def test_prod_app_no_catchall() -> None:
    app = create_app(dev=False)
    paths = _paths(app)
    assert "/" in paths
    assert "/{path:path}" not in paths  # no Vite catch-all in production


def test_prod_root_is_api_pointer_not_ui() -> None:
    """Headless: GET / returns a JSON pointer to how to get a console, not a page."""
    from starlette.testclient import TestClient

    app = create_app(dev=False)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 404
    body = r.json()
    assert body["ui"].startswith("none")
    assert any("--dev" in s for s in body["get_a_console"])


def test_dev_app_adds_vite_catchall() -> None:
    app = create_app(dev=True)
    paths = _paths(app)
    assert "/" in paths
    assert "/{path:path}" in paths  # frontend asset catch-all -> Vite
    # the `/` console route now proxies to Vite
    root = next(r for r in app.routes if isinstance(r, Route) and r.path == "/")
    assert root.endpoint is dev_server.proxy_to_vite
    # API routes still present and NOT proxied
    assert "/health" in paths and "/agent" in paths
