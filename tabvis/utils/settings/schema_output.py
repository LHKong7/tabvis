"""Settings JSON-Schema generation

Emits the JSON Schema for the settings file (used to publish/validate ``.tabvis/settings.json``).
The TS calls zod's ``toJSONSchema(SettingsSchema(), { unrepresentable: 'any' })``; the Python implementation
derives the schema from the pydantic :class:`~tabvis.utils.settings.types.SettingsJson` model via
``model_json_schema()``, serialized with the slow-operation-logged
:func:`~tabvis.utils.slow_operations.json_stringify` (indent 2), matching ``jsonStringify(.., null, 2)``.

The ``SettingsJson`` model is intentionally loose (``extra="allow"`` + a small set of explicit
fields), so the emitted schema is a faithful schema of *that* model, not a 1:1 reproduction of
zod's full ``SettingsSchema`` shape.
"""

from __future__ import annotations

from ..slow_operations import json_stringify
from .types import SettingsJson


def generate_settings_json_schema() -> str:
    """Return the settings JSON Schema as a 2-space-indented string (``generateSettingsJSONSchema``)."""
    json_schema = SettingsJson.model_json_schema()
    return json_stringify(json_schema, None, 2)
