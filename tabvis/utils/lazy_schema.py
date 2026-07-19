"""Lazy memoized factory

``lazySchema(factory)`` returns a zero-argument function that constructs the value
on first call and returns the same cached instance thereafter. The TS tree uses it
to defer Zod schema construction from module-init time to first access (and to
guarantee a single stable schema *reference* per session — see
``zodToJsonSchema``'s identity cache, which relies on that stability).

Faithful-behavior notes:
- The TS uses ``cached ??= factory()`` — the factory runs exactly once and the
  result is cached even if it is falsy. The Python implementation preserves that: a falsy
  result (``None``, ``0``, ``""``, ``[]``) is still cached and the factory is not
  re-invoked. (``functools.cache`` keyed on no args gives this exact semantics:
  one call, result memoized regardless of truthiness.)
- The returned getter is a true zero-arg callable, so the same getter object can be
  used as a stable identity key (parity with the TS reference-stability contract).

Casing: Python identifier snake_case (``lazy_schema``).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import TypeVar

T = TypeVar("T")


def lazy_schema(factory: Callable[[], T]) -> Callable[[], T]:
    """Return a memoized zero-arg getter that constructs the value on first call.

    The ``factory`` runs at most once; subsequent calls return the cached value
    (including a falsy value), matching the TS ``cached ??= factory()`` semantics.
    """

    @cache
    def getter() -> T:
        return factory()

    return getter
