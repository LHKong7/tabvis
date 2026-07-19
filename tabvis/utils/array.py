"""Small array helpers

Three dependency-free leaf utilities the TS tree uses to build lists:

- ``intersperse`` — weave a separator (computed from the index) between elements.
- ``count`` — count elements satisfying a predicate.
- ``uniq`` — de-duplicate an iterable, preserving first-seen order.

Casing: Python identifiers are snake_case; these operate on plain ``list``/iterables of
arbitrary values, so there are no wire-key dicts to preserve.

Faithful-behavior notes:
- The TS ``intersperse`` calls ``separator(i)`` with the *array index* of the element it
  precedes (so the first separator passed index 1, never 0 — index 0 is the falsy guard
  ``i ? ...`` that emits only the element). We reproduce that exactly.
- ``count`` mirrors ``n += +!!pred(x)``: each element contributes 1 when ``pred(x)`` is
  truthy, else 0. Python truthiness matches JS closely enough for the boolean coercion.
- ``uniq`` mirrors ``[...new Set(xs)]``. JS ``Set`` preserves insertion order; Python's
  ``dict.fromkeys`` does the same, so first-seen order is kept. Elements must be hashable
  (same constraint as JS ``Set`` membership on primitives).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

A = TypeVar("A")
T = TypeVar("T")


def intersperse(as_: Sequence[A], separator: Callable[[int], A]) -> list[A]:
    """Weave ``separator(index)`` between the elements of ``as_``.

    Mirrors ``as.flatMap((a, i) => (i ? [separator(i), a] : [a]))`` — for the first element
    (index 0) only the element is emitted; every later element ``a`` at index ``i`` is
    preceded by ``separator(i)``.
    """
    result: list[A] = []
    for i, a in enumerate(as_):
        if i:
            result.append(separator(i))
        result.append(a)
    return result


def count(arr: Sequence[T], pred: Callable[[T], object]) -> int:
    """Count the elements of ``arr`` for which ``pred(x)`` is truthy.

    Mirrors ``n += +!!pred(x)`` — each truthy predicate result contributes 1.
    """
    n = 0
    for x in arr:
        if pred(x):
            n += 1
    return n


def uniq(xs: Iterable[T]) -> list[T]:
    """De-duplicate ``xs`` (``[...new Set(xs)]``), preserving first-seen order.

    Elements must be hashable, matching the JS ``Set`` constraint for membership tests.
    """
    return list(dict.fromkeys(xs))
