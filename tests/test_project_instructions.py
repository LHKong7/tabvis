from __future__ import annotations

import asyncio
import os

from tabvis.agent import project_instructions
from tabvis.constants.prompts import get_system_prompt


def _set_project_paths(monkeypatch, root, cwd) -> None:
    monkeypatch.setattr(project_instructions, "get_project_root", lambda: str(root))
    monkeypatch.setattr(project_instructions, "get_cwd", lambda: str(cwd))
    monkeypatch.setattr(
        project_instructions,
        "get_additional_directories_for_tabvis_md",
        lambda: [],
    )


def test_discovers_repository_hierarchy_in_precedence_order(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "repo"
    cwd = root / "src" / "feature"
    cwd.mkdir(parents=True)
    (root / ".git").mkdir()
    _set_project_paths(monkeypatch, root, cwd)

    paths = project_instructions.discover_instruction_paths()

    assert paths == [
        str(root / "TABVIS.md"),
        str(root / "src" / "TABVIS.md"),
        str(cwd / "TABVIS.md"),
    ]


def test_extra_directories_follow_hierarchy_and_are_deduplicated(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "repo"
    cwd = root / "src"
    extra = tmp_path / "shared"
    cwd.mkdir(parents=True)
    extra.mkdir()
    _set_project_paths(monkeypatch, root, cwd)
    monkeypatch.setattr(
        project_instructions,
        "get_additional_directories_for_tabvis_md",
        lambda: [str(extra), str(cwd)],
    )

    paths = project_instructions.discover_instruction_paths(
        [str(extra), os.path.relpath(extra, cwd)]
    )

    assert paths == [
        str(root / "TABVIS.md"),
        str(cwd / "TABVIS.md"),
        str(extra / "TABVIS.md"),
    ]


def test_current_repository_is_used_after_moving_outside_session_project(
    monkeypatch, tmp_path
) -> None:
    original = tmp_path / "original"
    current_root = tmp_path / "current"
    cwd = current_root / "nested"
    original.mkdir()
    cwd.mkdir(parents=True)
    (current_root / ".git").mkdir()
    _set_project_paths(monkeypatch, original, cwd)

    paths = project_instructions.discover_instruction_paths()

    assert paths == [
        str(current_root / "TABVIS.md"),
        str(cwd / "TABVIS.md"),
    ]


def test_loads_only_existing_nonempty_utf8_files(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    cwd = root / "src" / "feature"
    cwd.mkdir(parents=True)
    _set_project_paths(monkeypatch, root, cwd)
    (root / "TABVIS.md").write_text("root guidance\n", encoding="utf-8")
    (root / "src" / "TABVIS.md").write_text("  \n", encoding="utf-8")
    (cwd / "TABVIS.md").write_bytes(b"\xff\xfe")

    loaded = asyncio.run(project_instructions.load_instruction_files())

    assert loaded == [
        {
            "path": str(root / "TABVIS.md"),
            "content": "root guidance\n",
            "truncated": False,
        }
    ]


def test_per_file_limit_preserves_valid_utf8(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _set_project_paths(monkeypatch, root, root)
    monkeypatch.setattr(project_instructions, "MAX_INSTRUCTION_BYTES", 10)
    (root / "TABVIS.md").write_text("界" * 10, encoding="utf-8")

    loaded = asyncio.run(project_instructions.load_instruction_files())

    assert len(loaded) == 1
    assert loaded[0]["content"] == "界" * 3
    assert loaded[0]["truncated"] is True
    assert loaded[0]["content"].encode("utf-8").decode("utf-8") == loaded[0]["content"]


def test_total_limit_prioritizes_more_specific_file(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    cwd = root / "src"
    cwd.mkdir(parents=True)
    _set_project_paths(monkeypatch, root, cwd)
    monkeypatch.setattr(project_instructions, "MAX_TOTAL_INSTRUCTION_BYTES", 8)
    (root / "TABVIS.md").write_text("123456", encoding="utf-8")
    (cwd / "TABVIS.md").write_text("abcdef", encoding="utf-8")

    loaded = asyncio.run(project_instructions.load_instruction_files())

    assert [item["content"] for item in loaded] == ["12", "abcdef"]
    assert [item["truncated"] for item in loaded] == [True, False]


def test_disable_environment_variable_skips_loading(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _set_project_paths(monkeypatch, root, root)
    (root / "TABVIS.md").write_text("Do the thing.\n", encoding="utf-8")
    monkeypatch.setenv(project_instructions.DISABLE_ENV_VAR, "true")

    prompt = asyncio.run(project_instructions.load_project_instructions_prompt())

    assert prompt is None


def test_system_prompt_includes_project_instructions(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _set_project_paths(monkeypatch, root, root)
    (root / "TABVIS.md").write_text("Use the project test command.\n", encoding="utf-8")
    monkeypatch.delenv("TABVIS_SIMPLE", raising=False)
    monkeypatch.delenv(project_instructions.DISABLE_ENV_VAR, raising=False)

    sections = asyncio.run(get_system_prompt([], "claude-sonnet-4-6"))

    project_section = next(
        section for section in sections if section.startswith("# Project instructions")
    )
    assert f"## `{root / 'TABVIS.md'}`" in project_section
    assert "Use the project test command." in project_section


def test_system_prompt_can_omit_project_instructions(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _set_project_paths(monkeypatch, root, root)
    (root / "TABVIS.md").write_text("Should not be loaded.\n", encoding="utf-8")
    monkeypatch.delenv("TABVIS_SIMPLE", raising=False)
    monkeypatch.delenv(project_instructions.DISABLE_ENV_VAR, raising=False)

    sections = asyncio.run(
        get_system_prompt(
            [],
            "claude-sonnet-4-6",
            include_project_instructions=False,
        )
    )

    assert not any(section.startswith("# Project instructions") for section in sections)
