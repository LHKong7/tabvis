"""Broker process hardening (design §5.7, §13.3, §17).

Best-effort process-level defenses applied at Broker/Executor startup:

* disable core dumps so a crash can't spill resolved secrets to disk (§5.7 "禁止 Core Dump", §17);
* scrub secret-bearing environment variables the Broker must not inherit or pass on (§17 "禁止 Broker
  Core Dump 和秘密环境变量继承");

These are defense-in-depth, not the isolation boundary (§2.3): real L2 isolation is a separate OS
identity / sandbox, which is a deployment concern. Every step degrades gracefully on platforms that do
not support it (e.g. Windows has no ``RLIMIT_CORE``).
"""

from __future__ import annotations

import os

from tabvis.utils.debug import log_for_debugging

# Env vars that may carry secret material and must not leak into the Broker's environment / crash dumps.
_SENSITIVE_ENV_SUBSTRINGS = (
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "APIKEY",
    "API_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
)
# Env vars this module must never scrub even if they match above (they are references / config, not
# plaintext secrets).
_KEEP = {
    "TABVIS_SECRET_BACKEND",
    "TABVIS_CREDENTIAL_BROKER_MODE",
    "TABVIS_CREDENTIAL_BROKER_ENDPOINT",
    "TABVIS_MANAGED_AUTH_REQUIRE_SECURE_SECRET_BACKEND",
}


def disable_core_dumps() -> bool:
    """Set the core-dump size limit to 0. Returns whether it was applied."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return True
    except Exception as exc:  # noqa: BLE001 - not available on all platforms
        log_for_debugging(f"[BROKER] could not disable core dumps: {type(exc).__name__}")
        return False


def scrub_secret_env(environ: dict[str, str] | None = None) -> list[str]:
    """Remove secret-bearing env vars from the process environment. Returns the removed names.

    Operates on ``os.environ`` by default. Names in :data:`_KEEP` are preserved (they are references /
    config, not plaintext).
    """
    env = environ if environ is not None else os.environ
    removed: list[str] = []
    for name in list(env.keys()):
        if name in _KEEP:
            continue
        upper = name.upper()
        if any(sub in upper for sub in _SENSITIVE_ENV_SUBSTRINGS):
            env.pop(name, None)
            removed.append(name)
    return removed


def apply_startup_hardening() -> dict[str, object]:
    """Apply all hardening steps at Broker startup. Returns a summary (safe to log — no secrets)."""
    core = disable_core_dumps()
    removed = scrub_secret_env()
    return {"core_dumps_disabled": core, "scrubbed_env_count": len(removed)}
