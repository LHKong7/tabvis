"""Deleting text must never silently join two lines.

``apply_edit_to_file`` absorbs the trailing newline of a deletion so removing a whole line does
not leave a blank one behind. That rule only holds for a match that spans an ENTIRE line; a
partial-line match (a trailing comment, a word) must keep its newline.
"""

from __future__ import annotations

import pytest

from tabvis.agent.tools.file_edit_tool import apply_edit_to_file


@pytest.mark.parametrize(
    ("content", "old_string", "expected"),
    [
        # Whole-line matches absorb the newline.
        ("keep\ndrop\nkeep2\n", "drop", "keep\nkeep2\n"),
        ("drop\nkeep\n", "drop", "keep\n"),
        ("keep\ndrop", "drop", "keep\n"),
        ("a\nb1\nb2\nc\n", "b1\nb2", "a\nc\n"),
        # Partial-line matches keep it.
        ("x = 1  # note\ny = 2\n", "  # note", "x = 1\ny = 2\n"),
        ("hello world\nnext\n", " world", "hello\nnext\n"),
    ],
)
def test_delete_absorbs_the_newline_only_for_whole_line_matches(
    content: str, old_string: str, expected: str
) -> None:
    assert apply_edit_to_file(content, old_string, "") == expected


def test_delete_replace_all_decides_per_occurrence() -> None:
    assert apply_edit_to_file("ax\nx\nb\n", "x", "", True) == "a\nb\n"


def test_delete_targets_the_first_plain_match() -> None:
    """A later whole-line occurrence must not retarget a non-``replace_all`` deletion."""
    assert apply_edit_to_file("alpha X beta\nX\ngamma\n", "X", "") == (
        "alpha  beta\nX\ngamma\n"
    )
