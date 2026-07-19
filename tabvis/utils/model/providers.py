"""API provider selection

Selects between the first-party model API and the Foundry path based on ``TABVIS_USE_FOUNDRY``,
and reports whether ``TABVIS_BASE_URL`` is configured for the direct model API path.

Casing: Python identifiers are snake_case; the ``APIProvider`` values (``'firstParty'`` /
``'foundry'``) are wire labels that flow to analytics, so they keep the TS camelCase spelling.
"""

from __future__ import annotations

import os
from typing import Literal

from tabvis.utils.env_utils import is_env_truthy

APIProvider = Literal["firstParty", "foundry"]


def get_api_provider() -> APIProvider:
    return "foundry" if is_env_truthy(os.environ.get("TABVIS_USE_FOUNDRY")) else "firstParty"


def get_api_provider_for_statsig() -> str:
    return get_api_provider()


def is_first_party_provider_base_url() -> bool:
    """Check if TABVIS_BASE_URL is configured for the direct model API path."""
    base_url = os.environ.get("TABVIS_BASE_URL")
    if not base_url:
        return False
    return True
