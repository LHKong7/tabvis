"""Settings validation + error formatting

Validates a settings-file body against the settings schema and formats validation issues into
human-readable :class:`ValidationError` records (enriched with the suggestions/doc-links from
:mod:`tabvis.utils.settings.validation_tips`). Also exposes
:func:`filter_invalid_permission_rules`, which strips bad ``permissions.{allow,deny,ask}`` entries
*in place* before schema validation so one bad rule doesn't poison the whole file.

Validation model: the :class:`~tabvis.utils.settings.types.SettingsJson` model is loose
(``extra="allow"``), so the schema-validation pass surfaces only the issues pydantic raises (type
errors on the explicitly-modelled fields). Strict full-schema validation (unknown-key
rejection, enum/min checks across ~50 keys) is not implemented in this build. The issue-mapping
machinery (:func:`format_zod_error`) runs against pydantic's ``error.errors()`` shape.

Casing: Python identifiers snake_case; :class:`ValidationError` keeps the wire field names
(``docLink`` / ``invalidValue`` / ``mcpErrorMetadata``) so the error payload round-trips verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ..slow_operations import json_parse
from ..string_utils import plural
from .permission_validation import validate_permission_rule
from .schema_output import generate_settings_json_schema
from .types import SettingsJson
from .validation_tips import TipContext, get_validation_tip

# Field path in dot notation (e.g. "permissions.defaultMode", "env.DEBUG").
FieldPath = str


@dataclass
class ValidationError:
    """A single settings-validation error.

    Keeps the TS wire field names (``docLink`` / ``invalidValue`` / ``mcpErrorMetadata``). The
    ``mcp_error_metadata`` field is only populated for MCP configuration errors (aggregated in
    :mod:`tabvis.utils.settings.all_errors`).
    """

    path: FieldPath
    message: str
    file: str | None = None
    expected: str | None = None
    invalid_value: Any = None  # wire name: invalidValue
    suggestion: str | None = None
    doc_link: str | None = None  # wire name: docLink
    mcp_error_metadata: dict[str, Any] | None = None  # wire name: mcpErrorMetadata


@dataclass
class SettingsWithErrors:
    """Merged settings + their validation errors."""

    settings: SettingsJson
    errors: list[ValidationError] = field(default_factory=list)


# --- Zod-issue helpers (implemented with pydantic ``error.errors()`` dicts) -------------------------------


def _get_received_type(value: Any) -> str:
    """Type string for an unknown value."""
    if value is None:
        # The TS distinguishes null vs undefined; both map to Python None. Mirror the JS ``null``
        # default (the ``undefined`` branch is only reachable from a missing-key issue, which the
        # message extraction below handles separately).
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


_RECEIVED_RE = re.compile(r"received (\w+)")


def _extract_received_from_message(msg: str) -> str | None:
    """Extract the received type token from a zod message."""
    match = _RECEIVED_RE.search(msg)
    return match.group(1) if match else None


def _is_invalid_type_issue(issue: dict[str, Any]) -> bool:
    return issue.get("code") == "invalid_type"


def _is_invalid_value_issue(issue: dict[str, Any]) -> bool:
    return issue.get("code") == "invalid_value"


def _is_unrecognized_keys_issue(issue: dict[str, Any]) -> bool:
    return issue.get("code") == "unrecognized_keys"


def _is_too_small_issue(issue: dict[str, Any]) -> bool:
    return issue.get("code") == "too_small"


def format_zod_error(error: PydanticValidationError, file_path: str) -> list[ValidationError]:
    """Format a validation error into :class:`ValidationError` records.

    Walks the issue list (pydantic ``error.errors()``, normalized to the zod-issue shape the TS
    code expects) and produces one :class:`ValidationError` per issue, enriched via
    :func:`~tabvis.utils.settings.validation_tips.get_validation_tip`.
    """
    return [_format_issue(issue, file_path) for issue in _normalize_issues(error)]


def _normalize_issues(error: PydanticValidationError) -> list[dict[str, Any]]:
    """Map pydantic ``error.errors()`` onto the zod-issue dict shape used below.

    pydantic types: ``string_type``/``int_type``/... -> ``invalid_type``;
    ``literal_error``/``enum`` -> ``invalid_value``; ``extra_forbidden`` -> ``unrecognized_keys``;
    ``greater_than_equal`` -> ``too_small``. Unknown types pass through with their raw code.
    """
    issues: list[dict[str, Any]] = []
    for err in error.errors():
        ptype = err.get("type", "")
        loc = err.get("loc", ())
        msg = err.get("msg", "")
        ctx = err.get("ctx") or {}
        inp = err.get("input")

        if ptype.endswith("_type") and ptype not in ("invalid_type",):
            expected = ptype[: -len("_type")]
            # pydantic's expected token (e.g. "string"/"int"/"bool") -> the JS typeof-ish name.
            expected = {"int": "number", "float": "number", "bool": "boolean"}.get(
                expected, expected
            )
            issues.append(
                {
                    "code": "invalid_type",
                    "path": list(loc),
                    "message": msg,
                    "expected": expected,
                    "input": inp,
                }
            )
        elif ptype in ("literal_error", "enum"):
            values = ctx.get("expected_values") or ctx.get("expected") or []
            if isinstance(values, str):
                values = [values]
            issues.append(
                {
                    "code": "invalid_value",
                    "path": list(loc),
                    "message": msg,
                    "values": list(values),
                    "input": inp,
                }
            )
        elif ptype == "extra_forbidden":
            issues.append(
                {
                    "code": "unrecognized_keys",
                    "path": list(loc[:-1]),
                    "message": msg,
                    "keys": [str(loc[-1])] if loc else [],
                }
            )
        elif ptype in ("greater_than_equal", "too_short"):
            issues.append(
                {
                    "code": "too_small",
                    "path": list(loc),
                    "message": msg,
                    "minimum": ctx.get("ge", ctx.get("min_length", 0)),
                    "origin": "number",
                }
            )
        elif ptype == "value_error":
            # Custom validator (e.g. permission-rule). Carry the failing input as ``received``.
            issues.append(
                {
                    "code": "custom",
                    "path": list(loc),
                    "message": msg,
                    "params": {"received": inp},
                }
            )
        else:
            issues.append({"code": ptype, "path": list(loc), "message": msg, "input": inp})
    return issues


def _format_issue(issue: dict[str, Any], file_path: str) -> ValidationError:
    """Format one normalized issue into a :class:`ValidationError` (the ``.map`` body of ``formatZodError``)."""
    path = ".".join(str(p) for p in issue.get("path", []))
    message = issue.get("message", "")
    expected: str | None = None

    enum_values: list[str] | None = None
    expected_value: str | None = None
    received_value: Any = None
    invalid_value: Any = None

    if _is_invalid_value_issue(issue):
        enum_values = [str(v) for v in issue.get("values", [])]
        expected_value = " | ".join(enum_values)
        received_value = None
        invalid_value = None
    elif _is_invalid_type_issue(issue):
        expected_value = issue.get("expected")
        received_type = _extract_received_from_message(issue.get("message", ""))
        received_value = received_type or _get_received_type(issue.get("input"))
        invalid_value = received_type or _get_received_type(issue.get("input"))
    elif _is_too_small_issue(issue):
        expected_value = str(issue.get("minimum"))
    elif issue.get("code") == "custom" and "params" in issue:
        params = issue.get("params") or {}
        received_value = params.get("received")
        invalid_value = received_value

    tip = get_validation_tip(
        TipContext(
            path=path,
            code=issue.get("code", ""),
            expected=expected_value,
            received=received_value,
            enum_values=enum_values,
            message=issue.get("message"),
            value=received_value,
        )
    )

    if _is_invalid_value_issue(issue):
        expected = ", ".join(f'"{v}"' for v in (enum_values or []))
        message = f"Invalid value. Expected one of: {expected}"
    elif _is_invalid_type_issue(issue):
        received_type = _extract_received_from_message(
            issue.get("message", "")
        ) or _get_received_type(issue.get("input"))
        if issue.get("expected") == "object" and received_type == "null" and path == "":
            message = "Invalid or malformed JSON"
        else:
            message = f"Expected {issue.get('expected')}, but received {received_type}"
    elif _is_unrecognized_keys_issue(issue):
        keys = ", ".join(issue.get("keys", []))
        message = f"Unrecognized {plural(len(issue.get('keys', [])), 'field')}: {keys}"
    elif _is_too_small_issue(issue):
        minimum = issue.get("minimum")
        message = f"Number must be greater than or equal to {minimum}"
        expected = str(minimum)

    return ValidationError(
        file=file_path,
        path=path,
        message=message,
        expected=expected,
        invalid_value=invalid_value,
        suggestion=tip.suggestion if tip else None,
        doc_link=tip.doc_link if tip else None,
    )


# --- file-content validation ----------------------------------------------------------------------


def validate_settings_file_content(content: str) -> dict[str, Any]:
    """Validate a settings-file body against the schema.

    Returns ``{"isValid": True}`` on success, else ``{"isValid": False, "error": str,
    "fullSchema": str}`` (wire keys ``isValid`` / ``fullSchema``).

    The :class:`~tabvis.utils.settings.types.SettingsJson` model is loose (``extra="allow"``), so
    strict unknown-key rejection is not enforced — only the explicitly-modelled fields' type
    checks run.
    """
    try:
        # Parse the JSON first.
        json_data = json_parse(content)
    except (ValueError, TypeError) as parse_error:
        return {
            "isValid": False,
            "error": f"Invalid JSON: {parse_error}",
            "fullSchema": generate_settings_json_schema(),
        }

    try:
        SettingsJson.model_validate(json_data)
    except PydanticValidationError as err:
        errors = format_zod_error(err, "settings")
        error_message = "Settings validation failed:\n" + "\n".join(
            f"- {e.path}: {e.message}" for e in errors
        )
        return {
            "isValid": False,
            "error": error_message,
            "fullSchema": generate_settings_json_schema(),
        }

    return {"isValid": True}


# --- permission-rule filtering --------------------------------------------------------------------


def filter_invalid_permission_rules(data: Any, file_path: str) -> list[ValidationError]:
    """Strip invalid ``permissions.{allow,deny,ask}`` rules from ``data`` in place.

    Mutates ``data["permissions"][key]`` to drop
    non-string and invalid rules, returning a :class:`ValidationError` warning for each removed
    entry. Returns ``[]`` when ``data`` has no ``permissions`` object.
    """
    if not data or not isinstance(data, dict):
        return []
    perms = data.get("permissions")
    if not perms or not isinstance(perms, dict):
        return []

    warnings: list[ValidationError] = []
    for key in ("allow", "deny", "ask"):
        rules = perms.get(key)
        if not isinstance(rules, list):
            continue

        kept: list[Any] = []
        for rule in rules:
            if not isinstance(rule, str):
                warnings.append(
                    ValidationError(
                        file=file_path,
                        path=f"permissions.{key}",
                        message=f"Non-string value in {key} array was removed",
                        invalid_value=rule,
                    )
                )
                continue
            result = validate_permission_rule(rule)
            if not result["valid"]:
                message = f'Invalid permission rule "{rule}" was skipped'
                if result.get("error"):
                    message += f": {result['error']}"
                if result.get("suggestion"):
                    message += f". {result['suggestion']}"
                warnings.append(
                    ValidationError(
                        file=file_path,
                        path=f"permissions.{key}",
                        message=message,
                        invalid_value=rule,
                    )
                )
                continue
            kept.append(rule)
        perms[key] = kept
    return warnings
