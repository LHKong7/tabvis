"""CLI parsing + validation for the Resume Plus flags (design §12.1)."""

from __future__ import annotations

import pytest

from tabvis.agent.main import _parse_args, main


def test_parses_resume_plus_and_flags() -> None:
    p = _parse_args(["-p", "go", "--resume-plus", "sess-1", "--conversation-only"])
    assert p["resume_plus"] == "sess-1" and p["conversation_only"] is True


def test_resume_equals_alias() -> None:
    assert _parse_args(["--resume=sess-2", "-p", "x"])["resume_plus"] == "sess-2"


def test_flags_default_off() -> None:
    p = _parse_args(["-p", "x"])
    assert p["resume_plus"] is None
    assert p["conversation_only"] is False and p["no_memory"] is False
    assert p["allow_new_browser"] is False


@pytest.mark.parametrize(
    "args",
    [
        ["-p", "x", "--conversation-only"],          # resume flag without --resume-plus
        ["-p", "x", "--no-memory"],
        ["-p", "x", "--allow-new-browser"],
        ["-p", "x", "--resume-plus", "s", "--conversation-only", "--no-memory"],  # conflicting
    ],
)
def test_invalid_flag_combos_exit(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    import asyncio

    monkeypatch.setattr("sys.argv", ["tabvis", *args])
    with pytest.raises(SystemExit) as ei:
        asyncio.run(main())
    assert ei.value.code == 2


def test_resume_plus_requires_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr("sys.argv", ["tabvis", "-p", "x", "--resume-plus", ""])
    with pytest.raises(SystemExit) as ei:
        asyncio.run(main())
    assert ei.value.code == 2
