"""Model capability cache

Reads/writes a small on-disk cache (``<config-home>/cache/model-capabilities.json``) of
``{id, max_input_tokens?, max_tokens?}`` rows fetched from the first-party ``models.list``
endpoint, and exposes a substring-matching lookup (:func:`get_model_capability`). Only
ant + first-party + first-party-base-URL sessions are eligible; everywhere else (including
the headless non-ant tree) the getter short-circuits to ``None`` and the refresh is a no-op.

Faithful-behavior notes:
- Zod ``.object({...}).strip()`` -> pydantic v2 ``model_config = {"extra": "ignore"}`` (drop
  internal-only fields like ``mycro_deployments`` rather than persist them).
- lodash ``memoize(loadCache, path => path)`` -> a module-level path-keyed dict cache with a
  ``.cache.delete(path)`` / ``.cache.clear()`` handle (parity with the TS cache API; this is
  the plain forever-memoize, NOT the TTL ``tabvis.utils.memoize`` variant).
- The wire keys (``id`` / ``max_input_tokens`` / ``max_tokens``) are Anthropic snake_case and
  are preserved verbatim on disk and in the returned dicts.

Casing: Python identifiers are snake_case; ``ModelCapability`` is a pydantic model whose
field names ARE the wire keys (snake already), so no aliasing is needed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from tabvis.agent.api.client import get_provider_client
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.json import safe_parse_json
from tabvis.utils.lazy_schema import lazy_schema
from tabvis.utils.privacy_level import is_essential_traffic_only
from tabvis.utils.slow_operations import json_stringify


# .strip() — don't persist internal-only fields (mycro_deployments etc.) to disk.
class ModelCapability(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    max_input_tokens: int | None = None
    max_tokens: int | None = None


class _CacheFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    models: list[ModelCapability]
    timestamp: float


# lazySchema parity — single stable schema reference per session (no-op for pydantic, kept
# for structural fidelity with the TS module).
_model_capability_schema = lazy_schema(lambda: ModelCapability)
_cache_file_schema = lazy_schema(lambda: _CacheFile)


def _get_cache_dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), "cache")


def _get_cache_path() -> str:
    return os.path.join(_get_cache_dir(), "model-capabilities.json")


def _is_model_capabilities_eligible() -> bool:
    return False


def _sort_for_matching(models: list[ModelCapability]) -> list[ModelCapability]:
    # Longest-id-first so substring match prefers most specific; secondary key for stable
    # equality (locale-naive lexicographic — parity with localeCompare for ASCII ids).
    return sorted(models, key=lambda m: (-len(m.id), m.id))


# --- path-keyed memoize (lodash memoize parity, NOT the TTL variant) -----------------------


class _LoadCacheHandle:
    """The ``loadCache.cache`` object — exposes ``delete``/``clear`` like a JS Map."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()


def _make_load_cache() -> Callable[[str], list[ModelCapability] | None]:
    store: dict[str, list[ModelCapability] | None] = {}

    def load_cache(path: str) -> list[ModelCapability] | None:
        if path in store:
            return store[path]
        result: list[ModelCapability] | None
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            parsed = safe_parse_json(raw, False)
            try:
                result = _CacheFile.model_validate(parsed).models
            except Exception:  # noqa: BLE001 - safeParse failure -> None (parity)
                result = None
        except OSError:
            result = None
        store[path] = result
        return result

    load_cache.cache = _LoadCacheHandle(store)  # type: ignore[attr-defined]
    return load_cache


load_cache = _make_load_cache()


def get_model_capability(model: str) -> ModelCapability | None:
    if not _is_model_capabilities_eligible():
        return None
    cached = load_cache(_get_cache_path())
    if not cached or len(cached) == 0:
        return None
    m = model.lower()
    for c in cached:
        if c.id.lower() == m:
            return c
    for c in cached:
        if c.id.lower() in m:
            return c
    return None


async def refresh_model_capabilities() -> None:
    if not _is_model_capabilities_eligible():
        return
    if is_essential_traffic_only():
        return

    try:
        model_client = await get_provider_client(max_retries=1)
        parsed: list[ModelCapability] = []
        async for entry in model_client.models.list():
            try:
                parsed.append(ModelCapability.model_validate(entry, from_attributes=True))
            except Exception:  # noqa: BLE001 - safeParse failure -> skip (parity)
                continue
        if len(parsed) == 0:
            return

        path = _get_cache_path()
        models = _sort_for_matching(parsed)
        prev = load_cache(path)
        if prev is not None and [m.model_dump() for m in prev] == [
            m.model_dump() for m in models
        ]:
            log_for_debugging("[modelCapabilities] cache unchanged, skipping write")
            return

        os.makedirs(_get_cache_dir(), exist_ok=True)
        payload = json_stringify(
            {
                "models": [m.model_dump(exclude_none=True) for m in models],
                "timestamp": int(time.time() * 1000),
            }
        )
        # mode=0o600 — owner read/write only (parity with the TS writeFile mode).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
        finally:
            pass
        load_cache.cache.delete(path)  # type: ignore[attr-defined]
        log_for_debugging(f"[modelCapabilities] cached {len(models)} models")
    except Exception as error:  # noqa: BLE001 - mirror the TS catch (best-effort cache refresh)
        message = str(error) if isinstance(error, Exception) else "unknown"
        log_for_debugging(f"[modelCapabilities] fetch failed: {message}")
