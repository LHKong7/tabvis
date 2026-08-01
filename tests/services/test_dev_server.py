"""Web console wiring for bundled production assets and the Vite development proxy."""

from __future__ import annotations

import os
from pathlib import Path

from starlette.routing import Route

from tabvis.browser import dev_server, server, web_console
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


def test_static_dir_defaults_to_packaged_bundle() -> None:
    assert web_console.static_dir() == Path(server.__file__).with_name("static")


def test_static_dir_uses_web_dir_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TABVIS_WEB_DIR", str(tmp_path / "web"))
    assert web_console.static_dir() == tmp_path / "web" / "dist"


def _built_console(monkeypatch, tmp_path) -> Path:
    web = tmp_path / "web"
    dist = web / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root">Tabvis console</div></body></html>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text('globalThis.tabvisConsole = true;\n', encoding="utf-8")
    monkeypatch.setenv("TABVIS_WEB_DIR", str(web))
    return dist


def test_prod_app_adds_static_spa_catchall() -> None:
    app = create_app(dev=False)
    paths = _paths(app)
    assert "/" in paths
    assert "/{path:path}" in paths


def test_prod_serves_root_assets_and_spa_routes(monkeypatch, tmp_path) -> None:
    from starlette.testclient import TestClient

    _built_console(monkeypatch, tmp_path)
    app = create_app(dev=False)
    with TestClient(app) as client:
        root = client.get("/")
        asset = client.get("/assets/app.js")
        spa = client.get("/sessions/run-123")
        missing_asset = client.get("/assets/missing.js")
        health = client.get("/health")
        missing_api = client.get("/v1/not-mounted")

    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert "Tabvis console" in root.text
    assert "no-cache" in root.headers["cache-control"]
    assert asset.status_code == 200
    assert "tabvisConsole" in asset.text
    assert spa.status_code == 200
    assert "Tabvis console" in spa.text
    assert missing_asset.status_code == 404
    assert health.status_code == 200
    assert health.json()["status"] == "ok"  # API route wins over the SPA catch-all
    assert missing_api.status_code == 404  # unknown API paths never receive the SPA shell


def test_prod_reports_missing_bundle(monkeypatch, tmp_path) -> None:
    from starlette.testclient import TestClient

    monkeypatch.setenv("TABVIS_WEB_DIR", str(tmp_path / "missing-web"))
    app = create_app(dev=False)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 503
    assert "npm run build" in response.text


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


def test_blocking_server_treats_keyboard_interrupt_as_clean_shutdown(monkeypatch) -> None:
    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(server.asyncio, "run", interrupted)

    server.serve()
