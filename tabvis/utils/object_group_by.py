"""``object_group_by``

A faithful re-implementation of the TC39 ``Object.groupBy`` proposal
(https://tc39.es/ecma262/multipage/fundamental-objects.html#sec-object.groupby): bucket the
items of an iterable by a key computed from ``(item, index)``, returning a mapping from key
to the list of items that produced it.

Casing: Python identifiers are snake_case; this groups arbitrary values into a plain ``dict``,
so there are no wire-key dicts to preserve.

Faithful-behavior notes:
- The TS source builds the accumulator with ``Object.create(null)`` (a prototype-less object)
  precisely so keys like ``"__proto__"`` or ``"constructor"`` can't collide with inherited
  members. A plain Python ``dict`` already has no such inherited string keys, so a regular
  ``{}`` reproduces the same safety. Insertion order of keys (first-seen) is preserved, as in
  both JS object key order and Python ``dict`` order.
- The key selector receives the running ``index`` (0-based), incremented per item — matching
  ``keySelector(item, index++)``.
- Keys must be hashable. TS ``PropertyKey`` is string/number/symbol; Python keys can be any
  hashable value, which is a strict superset and changes no in-tree behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from typing import TypeVar

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


def object_group_by(
    items: Iterable[T], key_selector: Callable[[T, int], K]
) -> dict[K, list[T]]:
    """Group ``items`` into ``{key: [items...]}`` keyed by ``key_selector(item, index)``."""
    result: dict[K, list[T]] = {}
    index = 0
    for item in items:
        key = key_selector(item, index)
        index += 1
        if key not in result:
            result[key] = []
        result[key].append(item)
    return result
