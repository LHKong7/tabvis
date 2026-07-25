"""CLI help should be a successful lightweight fast path."""

from __future__ import annotations

import asyncio
import sys

import pytest

from tabvis.ui.entry import cli


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_returns_success(monkeypatch, capsys, flag: str) -> None:
    monkeypatch.setattr(sys, "argv", ["tabvis", flag])

    asyncio.run(cli.main())

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("Usage:\n")
    assert "tabvis -p <prompt>" in captured.out
    assert "--serve" in captured.out


@pytest.mark.parametrize("arguments", [[], ["--serve"]])
def test_direct_start_and_serve_launch_web_service(monkeypatch, arguments: list[str]) -> None:
    called: dict[str, object] = {}

    async def fake_serve_async(*, host=None, port=None, dev=False) -> None:
        called.update(host=host, port=port, dev=dev)

    monkeypatch.setattr(sys, "argv", ["tabvis", *arguments])
    monkeypatch.delenv("TABVIS_WEB_DEV", raising=False)
    monkeypatch.setattr("tabvis.utils.config.enable_configs", lambda: None)
    monkeypatch.setattr("tabvis.browser.server.serve_async", fake_serve_async)

    asyncio.run(cli.main())

    assert called == {"host": None, "port": None, "dev": False}


def test_serve_options_are_forwarded(monkeypatch) -> None:
    called: dict[str, object] = {}

    async def fake_serve_async(*, host=None, port=None, dev=False) -> None:
        called.update(host=host, port=port, dev=dev)

    monkeypatch.setattr(
        sys,
        "argv",
        ["tabvis", "--serve", "--host", "0.0.0.0", "--port", "9000", "--dev"],
    )
    monkeypatch.setattr("tabvis.utils.config.enable_configs", lambda: None)
    monkeypatch.setattr("tabvis.browser.server.serve_async", fake_serve_async)

    asyncio.run(cli.main())

    assert called == {"host": "0.0.0.0", "port": 9000, "dev": True}
