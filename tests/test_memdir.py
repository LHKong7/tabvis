from __future__ import annotations

import asyncio
import os

from tabvis.agent.mem import memdir, paths
from tabvis.agent.mem.memdir import (
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    truncate_entrypoint_content,
)


def test_truncate_entrypoint_content_preserves_small_content() -> None:
    content = "- [User role](user_role.md) — preferences\n"

    result = truncate_entrypoint_content(content)

    assert result == {"content": content, "truncated": False}


def test_truncate_entrypoint_content_limits_lines() -> None:
    lines = [f"- memory {index}\n" for index in range(MAX_ENTRYPOINT_LINES + 5)]

    result = truncate_entrypoint_content("".join(lines))

    assert result["content"] == "".join(lines[:MAX_ENTRYPOINT_LINES])
    assert result["truncated"] is True


def test_truncate_entrypoint_content_limits_utf8_bytes() -> None:
    content = "界" * (MAX_ENTRYPOINT_BYTES // len("界".encode("utf-8")) + 10)

    result = truncate_entrypoint_content(content)

    assert len(result["content"].encode("utf-8")) <= MAX_ENTRYPOINT_BYTES
    assert result["content"].encode("utf-8").decode("utf-8") == result["content"]
    assert result["truncated"] is True


def test_load_memory_prompt_reads_truncated_entrypoint(monkeypatch, tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    lines = [f"- entry {index}\n" for index in range(MAX_ENTRYPOINT_LINES + 5)]
    (memory_dir / "MEMORY.md").write_text("".join(lines), encoding="utf-8")
    monkeypatch.setattr(memdir, "is_auto_memory_enabled", lambda: True)
    monkeypatch.setattr(memdir, "get_auto_mem_path", lambda: str(memory_dir) + os.sep)

    prompt = asyncio.run(memdir.load_memory_prompt())

    assert prompt is not None
    assert "".join(lines[:MAX_ENTRYPOINT_LINES]).rstrip() in prompt
    assert lines[MAX_ENTRYPOINT_LINES].rstrip() not in prompt
    assert "MEMORY.md truncated to 200 lines / 40000 UTF-8 bytes" in prompt


def test_memory_path_env_override_has_highest_precedence(
    monkeypatch, tmp_path
) -> None:
    env_override = tmp_path / "env-memory"
    setting_override = tmp_path / "setting-memory"
    monkeypatch.setenv("TABVIS_MEMORY_PATH_OVERRIDE", str(env_override))
    monkeypatch.setattr(
        paths,
        "_get_auto_mem_path_setting",
        lambda: str(setting_override) + os.sep,
    )

    assert paths.has_auto_mem_path_override() is True
    assert paths.get_auto_mem_path() == str(env_override) + os.sep


def test_invalid_memory_path_env_override_falls_back_to_setting(
    monkeypatch, tmp_path
) -> None:
    setting_override = tmp_path / "setting-memory"
    monkeypatch.setenv("TABVIS_MEMORY_PATH_OVERRIDE", "relative/memory")
    monkeypatch.setattr(
        paths,
        "_get_auto_mem_path_setting",
        lambda: str(setting_override) + os.sep,
    )

    assert paths.has_auto_mem_path_override() is False
    assert paths.get_auto_mem_path() == str(setting_override) + os.sep
