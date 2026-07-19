"""Model selection

Skeleton scope: the resolution chain ``get_main_loop_model`` (TABVIS_MODEL env / settings /
built-in default), the default-tier getters, alias parsing, canonical naming, and
``normalize_model_string_for_api``. Provider is assumed first-party; the GrowthBook/Foundry
provider matrix and settings overrides are planned for a later implementation phase.
"""

from __future__ import annotations

import os
import re

ModelName = str
ModelShortName = str
ModelAlias = str

# firstParty column of ALL_MODEL_CONFIGS (src/utils/model/configs.ts).
_MODEL_STRINGS_FIRST_PARTY: dict[str, str] = {
    "sonnet37": "claude-3-7-sonnet-20250219",
    "sonnet35": "claude-3-5-sonnet-20241022",
    "haiku35": "claude-3-5-haiku-20241022",
    "haiku45": "claude-haiku-4-5-20251001",
    "sonnet40": "claude-sonnet-4-20250514",
    "sonnet45": "claude-sonnet-4-5-20250929",
    "opus40": "claude-opus-4-20250514",
    "opus41": "claude-opus-4-1-20250805",
    "opus45": "claude-opus-4-5-20251101",
    "opus46": "claude-opus-4-6",
    "sonnet46": "claude-sonnet-4-6",
}


def get_model_strings() -> dict[str, str]:
    # Skeleton: first-party provider only; settings modelOverrides applied in a later wave.
    return dict(_MODEL_STRINGS_FIRST_PARTY)


def get_default_opus_model() -> ModelName:
    return os.environ.get("TABVIS_DEFAULT_OPUS_MODEL") or get_model_strings()["opus46"]


def get_default_sonnet_model() -> ModelName:
    return os.environ.get("TABVIS_DEFAULT_SONNET_MODEL") or get_model_strings()["sonnet46"]


def get_default_haiku_model() -> ModelName:
    return os.environ.get("TABVIS_DEFAULT_HAIKU_MODEL") or get_model_strings()["haiku45"]


# Aliases users can pass (subset; tabvis-* tiers + 1m variants).
_ALIAS_TO_RESOLVER = {
    "tabvis-balanced": get_default_sonnet_model,
    "tabvis-max": get_default_opus_model,
    "tabvis-fast": get_default_haiku_model,
    "tabvis-plan": get_default_opus_model,
    "sonnet": get_default_sonnet_model,
    "opus": get_default_opus_model,
    "haiku": get_default_haiku_model,
}


def parse_user_specified_model(model: str | None) -> ModelName:
    """Resolve a user model setting/alias to a concrete model string."""
    if not model:
        return get_default_sonnet_model()
    base = model
    suffix = ""
    m = re.search(r"\[(1|2)m\]$", base, re.IGNORECASE)
    if m:
        suffix = base[m.start():]
        base = base[: m.start()]
    resolver = _ALIAS_TO_RESOLVER.get(base)
    resolved = resolver() if resolver else base
    return resolved + suffix


def get_user_specified_model_setting() -> ModelName | None:
    # Precedence: TABVIS_MODEL env > settings.model > undefined (model.ts:57).
    # Lazy import keeps the settings layer out of this module's import graph (cycle avoidance).
    from tabvis.utils.settings.settings import get_initial_settings

    return os.environ.get("TABVIS_MODEL") or get_initial_settings().model or None


def get_default_main_loop_model_setting() -> str:
    # Non-ant (env/API-key auth) default = TABVIS Balanced (sonnet46).
    return get_default_sonnet_model()


def get_default_main_loop_model() -> ModelName:
    return parse_user_specified_model(get_default_main_loop_model_setting())


def get_main_loop_model() -> ModelName:
    model = get_user_specified_model_setting()
    if model is not None:
        return parse_user_specified_model(model)
    return get_default_main_loop_model()


def get_best_model() -> ModelName:
    return get_default_opus_model()


def first_party_name_to_canonical(name: ModelName) -> ModelShortName:
    """Strip date/provider suffixes from a first-party model id (order: specific first)."""
    name = name.lower()
    for needle, canonical in (
        ("claude-opus-4-6", "claude-opus-4-6"),
        ("claude-opus-4-5", "claude-opus-4-5"),
        ("claude-opus-4-1", "claude-opus-4-1"),
        ("claude-opus-4", "claude-opus-4"),
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("claude-sonnet-4", "claude-sonnet-4"),
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-3-7-sonnet", "claude-3-7-sonnet"),
        ("claude-3-5-sonnet", "claude-3-5-sonnet"),
        ("claude-3-5-haiku", "claude-3-5-haiku"),
    ):
        if needle in name:
            return canonical
    return name


def get_canonical_name(full_model_name: ModelName) -> ModelShortName:
    return first_party_name_to_canonical(full_model_name)


def normalize_model_string_for_api(model: str) -> str:
    """Strip the [1m]/[2m] context-window suffix before sending to the API."""
    return re.sub(r"\[(1|2)m\]", "", model, flags=re.IGNORECASE)


def get_marketing_name_for_model(model_id: str) -> str | None:
    """Map a model id to its TABVIS marketing name (None if unrecognized).

    Skeleton: provider is assumed first-party (the Foundry branch — which returns None for
    user-defined deployment ids — is implemented with the provider matrix in a later wave).
    """
    has_1m = "[1m]" in model_id.lower()
    canonical = get_canonical_name(model_id)

    if "claude-opus-4-6" in canonical:
        return "TABVIS Max 4.6 (with 1M context)" if has_1m else "TABVIS Max 4.6"
    if "claude-opus-4-5" in canonical:
        return "TABVIS Max 4.5"
    if "claude-opus-4-1" in canonical:
        return "TABVIS Max 4.1"
    if "claude-opus-4" in canonical:
        return "TABVIS Max 4"
    if "claude-sonnet-4-6" in canonical:
        return "TABVIS Balanced 4.6 (with 1M context)" if has_1m else "TABVIS Balanced 4.6"
    if "claude-sonnet-4-5" in canonical:
        return "TABVIS Balanced 4.5 (with 1M context)" if has_1m else "TABVIS Balanced 4.5"
    if "claude-sonnet-4" in canonical:
        return "TABVIS Balanced 4 (with 1M context)" if has_1m else "TABVIS Balanced 4"
    if "claude-3-7-sonnet" in canonical:
        return "TABVIS Balanced 3.7"
    if "claude-3-5-sonnet" in canonical:
        return "TABVIS Balanced 3.5"
    if "claude-haiku-4-5" in canonical:
        return "TABVIS Fast 4.5"
    if "claude-3-5-haiku" in canonical:
        return "TABVIS Fast 3.5"
    return None
