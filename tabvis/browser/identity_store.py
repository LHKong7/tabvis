"""Durable, agent-keyed BrowserIdentity store (IDP-2).

Promotes the identity *vocabulary* (``identity.py``) into a durable store keyed by the stable
``agent_id``, implementing the design's ``identity.resolve`` / ``getByAgent`` / ``updateForAgent``
verbs (``design.md`` §1 "Runtime API"). An agent has exactly one identity: :func:`resolve` returns
it, creating it on first use — the design's resolve-or-create.

Storage follows the same pattern as the rest of Phase 2: a JSON sidecar under
``<config-home>/browser-identities/<agent_id>.json`` is the source of truth, mirrored best-effort to
the SQLite ``browser_identities`` table (``agent_id`` UNIQUE). resolve-or-create runs inside a
process lock with no ``await`` between the check and the create, so a single-process runtime cannot
double-create — the in-process equivalent of the design's "single DB transaction" requirement
(a real transactional guarantee arrives with the multi-writer split, RT-9).

Only ``*_ref`` fields ever hold anything sensitive, and even those are references — the record is
safe to hand back as ``IdentityMetadata``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from tabvis.browser.identity import (
    BrowserIdentity,
    IdentityAuth,
    IdentityBinding,
    IdentityEnvironment,
    IdentityNetwork,
    IdentityPermissions,
    IdentityProfile,
    new_identity_id,
)
from tabvis.browser.session import utc_now
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_IDENTITIES_DIRNAME = "browser-identities"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")

_lock = threading.RLock()
_cache: dict[str, BrowserIdentity] = {}


def identities_dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), _IDENTITIES_DIRNAME)


def _slug(agent_id: str) -> str:
    slug = _SLUG_RE.sub("-", agent_id.strip()).strip(".-")
    return slug[:128] or "agent"


def _path(agent_id: str) -> str:
    return os.path.join(identities_dir(), f"{_slug(agent_id)}.json")


def _from_dict(d: dict[str, Any]) -> BrowserIdentity:
    return BrowserIdentity(
        agent_id=d["agent_id"],
        id=d.get("id") or new_identity_id(),
        name=d.get("name"),
        status=d.get("status", "ready"),
        profile=IdentityProfile(**(d.get("profile") or {})),
        auth=IdentityAuth(**(d.get("auth") or {})),
        network=IdentityNetwork(**(d.get("network") or {})),
        environment=IdentityEnvironment(**(d.get("environment") or {})),
        permissions=IdentityPermissions(**(d.get("permissions") or {})),
        created_at=d.get("created_at") or utc_now(),
        last_used_at=d.get("last_used_at"),
        updated_at=d.get("updated_at"),
    )


def _save(identity: BrowserIdentity) -> None:
    """Write the JSON sidecar (source of truth) and mirror best-effort to SQLite."""
    identity.updated_at = utc_now()
    data = identity.to_dict()
    try:
        os.makedirs(identities_dir(), exist_ok=True)
        path = _path(identity.agent_id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log_for_debugging(f"[IDENTITY] failed to write sidecar for {identity.agent_id}: {e}")
    try:
        from tabvis.browser.persistence import db

        db.upsert_identity(data)
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[IDENTITY] failed to mirror {identity.agent_id} to sqlite: {e}")


def _load(agent_id: str) -> BrowserIdentity | None:
    """Load a persisted identity: JSON sidecar first, then the SQLite mirror. None if neither."""
    try:
        with open(_path(agent_id), encoding="utf-8") as fh:
            return _from_dict(json.load(fh))
    except (OSError, ValueError, KeyError):
        pass
    try:
        from tabvis.browser.persistence import db

        data = db.get_identity_by_agent(agent_id)
        if data:
            return _from_dict(data)
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve(agent_id: str, *, profile_ref: str | None = None) -> BrowserIdentity:
    """Return the agent's identity, creating it on first use (resolve-or-create).

    ``profile_ref`` (the Chromium user-data dir today) seeds a new identity's profile and backfills an
    existing one that has none, so the identity becomes the record of where the agent's profile lives.
    """
    with _lock:
        identity = _cache.get(agent_id) or _load(agent_id)
        if identity is None:
            identity = BrowserIdentity(agent_id=agent_id)
            if profile_ref:
                identity.profile.profile_ref = profile_ref
            _cache[agent_id] = identity
            _save(identity)
            return identity
        _cache[agent_id] = identity
        if profile_ref and not identity.profile.profile_ref:
            identity.profile.profile_ref = profile_ref
            _save(identity)
        return identity


def get_by_agent(agent_id: str) -> BrowserIdentity | None:
    """The agent's identity metadata, or None if it has never been resolved. Does not create."""
    with _lock:
        return _cache.get(agent_id) or _load(agent_id)


def update_for_agent(agent_id: str, patch: dict[str, Any]) -> BrowserIdentity:
    """Apply a shallow patch to the agent's identity and persist it (resolve-or-create first).

    Top-level scalar fields (``name`` / ``status``) are set directly; the nested sub-objects
    (``environment`` / ``network`` / ``permissions`` / ``profile`` / ``auth``) are updated field-wise
    from a dict so a caller can patch just one attribute. ``agent_id`` / ``id`` are immutable owners
    and are ignored if present in the patch.
    """
    with _lock:
        identity = resolve(agent_id)
        _sub = {
            "profile": identity.profile,
            "auth": identity.auth,
            "network": identity.network,
            "environment": identity.environment,
            "permissions": identity.permissions,
        }
        for key, value in patch.items():
            if key in ("agent_id", "id"):
                continue
            if key in _sub and isinstance(value, dict):
                for attr, attr_val in value.items():
                    if hasattr(_sub[key], attr):
                        setattr(_sub[key], attr, attr_val)
            elif hasattr(identity, key):
                setattr(identity, key, value)
        _save(identity)
        return identity


# --------------------------------------------------------------------------- IDP-4: bindings

# A transient IdentityBinding per acquisition (design.md Runtime Binding). This is metadata over the
# real exclusivity lock, which manager's owner_agent/busy_agent still enforce — acquire/release just
# record the binding and flip the identity's status ready↔in_use.
_bindings: dict[str, IdentityBinding] = {}       # binding_id -> binding
_binding_by_agent: dict[str, str] = {}           # agent_id   -> binding_id


def acquire(agent_id: str, workspace_id: str | None = None) -> IdentityBinding:
    """Acquire an :class:`IdentityBinding` for a run; flips the identity to ``in_use`` (IDP-4)."""
    with _lock:
        identity = resolve(agent_id)
        identity.status = "in_use"
        identity.last_used_at = utc_now()
        _save(identity)
        binding = IdentityBinding(
            identity_id=identity.id, agent_id=agent_id, workspace_id=workspace_id
        )
        _bindings[binding.binding_id] = binding
        _binding_by_agent[agent_id] = binding.binding_id
        return binding


def refresh(binding_id: str, *, expires_at: str | None = None) -> IdentityBinding | None:
    """Renew a binding's lease (IDP-4). None if the binding is unknown."""
    with _lock:
        binding = _bindings.get(binding_id)
        if binding is None:
            return None
        if expires_at is not None:
            binding.expires_at = expires_at
        return binding


def release(binding_id: str, *, persist: bool = True) -> None:
    """Retire a binding and flip its identity back to ``ready`` (IDP-4). No-op if unknown."""
    with _lock:
        binding = _bindings.pop(binding_id, None)
        if binding is None:
            return
        _binding_by_agent.pop(binding.agent_id, None)
        identity = get_by_agent(binding.agent_id)
        if identity is not None:
            identity.status = "ready"
            if persist:
                _save(identity)


def release_for_agent(agent_id: str, *, persist: bool = True) -> None:
    """Release whatever binding an agent currently holds (used at close). No-op if none."""
    with _lock:
        binding_id = _binding_by_agent.get(agent_id)
        if binding_id:
            release(binding_id, persist=persist)


def get_binding(binding_id: str) -> IdentityBinding | None:
    return _bindings.get(binding_id)


def get_binding_for_agent(agent_id: str) -> IdentityBinding | None:
    binding_id = _binding_by_agent.get(agent_id)
    return _bindings.get(binding_id) if binding_id else None


# --------------------------------------------------------------------------- IDP-5: launch overlay


def launch_overlay(agent_id: str | None) -> dict[str, Any]:
    """Per-identity environment/network overlay for browser launch (IDP-5).

    Returns ONLY the fields the identity actually sets (Playwright context-option names), so a fresh
    identity — whose environment/network are all empty — overlays nothing and the default launch is
    byte-for-byte unchanged. Applied for every engine by ``BrowserService`` at launch.
    """
    if not agent_id:
        return {}
    identity = get_by_agent(agent_id)
    if identity is None:
        return {}
    env = identity.environment
    net = identity.network
    overlay: dict[str, Any] = {}
    if env.user_agent:
        overlay["user_agent"] = env.user_agent
    if env.locale:
        overlay["locale"] = env.locale
    if env.timezone:
        overlay["timezone_id"] = env.timezone
    if env.viewport:
        overlay["viewport"] = env.viewport
    if net.proxy_ref:
        # proxy_ref may be a secret_ref (IDP-6) or, for back-compat, a raw URL set directly.
        resolved = _resolve_secret(net.proxy_ref)
        if resolved:
            overlay["proxy"] = resolved
        elif "://" in net.proxy_ref:
            overlay["proxy"] = net.proxy_ref  # a raw URL was stored directly (not a secret_ref)
        # else: a sec_-shaped ref that failed to resolve — OMIT the proxy rather than route every
        # navigation through a bogus host literally named after the ref token.
    return overlay


# --------------------------------------------------------------------------- IDP-6: secrets


def _resolve_secret(ref: str | None) -> str | None:
    if not ref:
        return None
    try:
        from tabvis.browser import secret_store

        return secret_store.get(ref)
    except Exception:  # noqa: BLE001
        return None


def store_credential(agent_id: str, value: str) -> str:
    """Store a credential secret and attach its ``secret_ref`` to the identity's auth (IDP-6).

    The plaintext goes to the secret store; the identity keeps only the ref. Returns the ref.
    """
    from tabvis.browser import secret_store

    with _lock:
        identity = resolve(agent_id)
        ref = secret_store.put(value)
        if ref not in identity.auth.credential_refs:
            identity.auth.credential_refs.append(ref)
        _save(identity)
        return ref


def set_proxy(agent_id: str, proxy_url: str) -> str:
    """Store the identity's proxy URL as a secret and set ``network.proxy_ref`` (IDP-6). Returns ref."""
    from tabvis.browser import secret_store

    with _lock:
        identity = resolve(agent_id)
        ref = secret_store.put(proxy_url)
        identity.network.proxy_ref = ref
        _save(identity)
        return ref


def store_storage_state(agent_id: str, storage_state: dict[str, Any]) -> str:
    """Store a Playwright ``storage_state`` (cookies + storage) as a secret and set

    ``auth.storage_state_ref`` (IDP-6 / the design's ``storage-state.enc``). Returns the ref.
    """
    import json

    from tabvis.browser import secret_store

    with _lock:
        identity = resolve(agent_id)
        ref = secret_store.put(json.dumps(storage_state, default=str))
        identity.auth.storage_state_ref = ref
        _save(identity)
        return ref


def load_storage_state(agent_id: str) -> dict[str, Any] | None:
    """Resolve the identity's stored ``storage_state`` back to a dict, or None (IDP-6)."""
    import json

    identity = get_by_agent(agent_id)
    if identity is None or not identity.auth.storage_state_ref:
        return None
    raw = _resolve_secret(identity.auth.storage_state_ref)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def resolve_credential(secret_ref: str) -> str | None:
    """Resolve a credential ``secret_ref`` to its plaintext — for runtime injection ONLY (IDP-7)."""
    return _resolve_secret(secret_ref)
