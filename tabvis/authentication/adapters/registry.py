"""Static, versioned adapter registry (design §9.1).

Adapters are looked up by a **version-suffixed name** from a static table. A credential profile can
only name an adapter that is registered here — it can never point at an arbitrary Python module, file
path or script (design §9.1 "Profile 不能指定任意 Python 模块、文件路径或脚本"). Adding or updating an
adapter is a code change subject to review, not runtime configuration.
"""

from __future__ import annotations

from tabvis.authentication.adapters.base import AuthenticationAdapter
from tabvis.authentication.adapters.generic_password import GenericPasswordAdapter

# The static registry. Keys are version-suffixed adapter names (§9.1). Factories are called per use so
# each authentication gets a fresh, stateless adapter instance.
_ADAPTERS: dict[str, type[AuthenticationAdapter]] = {
    "generic_password_v1": GenericPasswordAdapter,
}


def is_registered_adapter(name: str) -> bool:
    return name in _ADAPTERS


def get_adapter(name: str) -> AuthenticationAdapter:
    """Instantiate a registered adapter by name, or raise ``KeyError`` for an unknown name.

    An unknown adapter is a hard failure, never a fallback — a profile referencing an unregistered
    adapter must fail closed rather than run some default login flow.
    """
    try:
        factory = _ADAPTERS[name]
    except KeyError:
        raise KeyError(f"unknown authentication adapter: {name!r}") from None
    return factory()


def registered_adapter_names() -> list[str]:
    return sorted(_ADAPTERS)
