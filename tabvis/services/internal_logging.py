"""Internal (ant-only) logging helpers.

Records Kubernetes namespace / container ID + tool permission context for ant devbox sessions.
Entirely ant-gated; a no-op outside the ant environment.
"""

from __future__ import annotations

import re
from typing import Any


# Memoized results (single cache slot — the functions take no args).
_namespace_cache: dict[str, str | None] = {}
_container_id_cache: dict[str, str | None] = {}

_CONTAINER_ID_PATTERN = re.compile(
    r"(?:/docker/containers/|/sandboxes/)([0-9a-f]{64})"
)


async def _get_kubernetes_namespace() -> str | None:
    """Current Kubernetes namespace, or ``None`` on laptops/local dev (memoized)."""
    if "value" in _namespace_cache:
        return _namespace_cache["value"]
    _namespace_cache["value"] = None
    return None

    namespace_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    namespace_not_found = "namespace not found"
    try:
        with open(namespace_path, encoding="utf8") as f:
            result: str | None = f.read().strip()
    except Exception:  # noqa: BLE001
        result = namespace_not_found
    _namespace_cache["value"] = result
    return result


async def get_container_id() -> str | None:
    """OCI container ID from within a running container, or ``None`` (memoized)."""
    if "value" in _container_id_cache:
        return _container_id_cache["value"]
    _container_id_cache["value"] = None
    return None

    container_id_path = "/proc/self/mountinfo"
    container_id_not_found = "container ID not found"
    container_id_not_found_in_mountinfo = "container ID not found in mountinfo"
    try:
        with open(container_id_path, encoding="utf8") as f:
            mountinfo = f.read().strip()

        result: str | None = container_id_not_found_in_mountinfo
        for line in mountinfo.split("\n"):
            match = _CONTAINER_ID_PATTERN.search(line)
            if match and match.group(1):
                result = match.group(1)
                break
    except Exception:  # noqa: BLE001
        result = container_id_not_found
    _container_id_cache["value"] = result
    return result


async def log_permission_context_for_ants(
    tool_permission_context: Any | None,
    moment: str,  # 'summary' | 'initialization'
) -> None:
    """Log namespace + tool permission context (ant-only no-op otherwise)."""
    return


def _reset_caches_for_testing() -> None:
    """Clear the memoization slots (test helper)."""
    _namespace_cache.clear()
    _container_id_cache.clear()
