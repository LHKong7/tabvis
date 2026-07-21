"""Version constraint checking (design §8.5).

A tiny, dependency-free constraint evaluator for the manifest's ``requires.tabvis`` (and, later, plugin
version pins). Supports comma-ANDed comparators (``>=0.4,<0.6``), the operators ``>= > <= < == !=``, and
a bare ``*`` wildcard. Versions compare numerically component-by-component; a non-numeric suffix
(``1.2.0rc1``) is truncated to its numeric prefix — enough for the design's protocol-version needs
without pulling in a packaging dependency.
"""

from __future__ import annotations

import re

_OPERATOR_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<)?\s*([0-9][0-9.]*)\s*$")


def _parse_version(v: str) -> tuple[int, ...]:
    nums: list[int] = []
    for part in v.split("."):
        m = re.match(r"\d+", part)
        nums.append(int(m.group()) if m else 0)
    return tuple(nums) or (0,)


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def _satisfies_one(version: tuple[int, ...], comparator: str) -> bool:
    comparator = comparator.strip()
    if comparator in ("", "*"):
        return True
    m = _OPERATOR_RE.match(comparator)
    if not m:
        return False
    op, ref = m.group(1) or ">=", m.group(2)
    c = _cmp(version, _parse_version(ref))
    return {
        ">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0, "==": c == 0, "!=": c != 0,
    }[op]


def satisfies(version: str, constraint: str) -> bool:
    """True iff ``version`` satisfies every comma-separated comparator in ``constraint``."""
    if not constraint or constraint.strip() == "*":
        return True
    v = _parse_version(version)
    return all(_satisfies_one(v, part) for part in constraint.split(","))
