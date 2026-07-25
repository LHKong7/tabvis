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
