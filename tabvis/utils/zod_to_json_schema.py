"""Zod → JSON Schema conversion

The TS module converts a Zod v4 schema to a JSON Schema 7 object via Zod's native
``toJSONSchema``, caching the result in a ``WeakMap`` keyed by schema *identity*.
The identity cache is sound because tool schemas are wrapped with ``lazySchema()``,
which guarantees the same schema reference per session; ``toolToAPISchema()`` runs
this ~60-250 times/turn, so the cache matters.

Implementation mapping (per the plan: Zod → pydantic v2):
- A "schema" here is a pydantic ``BaseModel`` subclass. The JSON Schema is produced
  by ``schema.model_json_schema(by_alias=True)`` — ``by_alias`` so camelCase wire
  keys declared via ``Field(alias=...)`` surface in the emitted schema (the
  Anthropic API consumes the wire keys verbatim).
- The ``WeakMap<ZodTypeAny, JsonSchema7Type>`` becomes a
  ``WeakKeyDictionary`` keyed on the model class' identity. Like the TS WeakMap,
  entries do not keep the schema alive and are dropped when it is collected. This
  is identity caching, *not* ``lru_cache`` (which would key on equality / require
  hashable args and could not distinguish two structurally-equal-but-distinct
  schemas) — matching the WeakMap-by-reference contract exactly.

Some schema objects (e.g. a few pydantic generics) are not weak-referenceable; in
that case the conversion still runs, just uncached (parity: a WeakMap silently
no-ops for non-registerable keys rather than throwing).

Casing: Python identifier snake_case (``zod_to_json_schema``); the returned dict is
JSON-Schema wire data kept verbatim.
"""

from __future__ import annotations

from typing import Any
from weakref import WeakKeyDictionary

# JsonSchema7Type — the TS alias ``Record<string, unknown>``.
JsonSchema7Type = dict[str, Any]

# toolToAPISchema() runs this for every tool on every API request. Schemas are
# wrapped with lazy_schema() which guarantees the same model reference per session,
# so caching by identity is sound. WeakKeyDictionary mirrors the TS WeakMap: keyed
# by reference, entries collected with the schema.
_cache: WeakKeyDictionary[Any, JsonSchema7Type] = WeakKeyDictionary()


def zod_to_json_schema(schema: Any) -> JsonSchema7Type:
    """Convert a pydantic ``BaseModel`` schema to a JSON Schema 7 dict (identity-cached).

    ``schema`` is the pydantic model class that stands in for the TS ``ZodTypeAny``.
    The result of ``model_json_schema(by_alias=True)`` is cached by the schema's
    identity (parity with the TS ``WeakMap`` keyed by reference).
    """
    try:
        hit = _cache.get(schema)
    except TypeError:
        # Non-weak-referenceable schema: skip the cache (compute uncached).
        hit = None
    if hit is not None:
        return hit

    result: JsonSchema7Type = schema.model_json_schema(by_alias=True)

    try:
        _cache[schema] = result
    except TypeError:
        # Non-weak-referenceable key — WeakMap.set would also be a no-op here.
        pass
    return result
