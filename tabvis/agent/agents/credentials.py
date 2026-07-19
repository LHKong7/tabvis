"""Local agent credentials + Agent Context (RT-3).

``design.md`` §"Runtime API 形态": an agent registers and receives a local Credential; the Runtime API
builds an Agent Context from it and reads ``agent_id`` from there, so a business request cannot spoof
``agent_id``. This is the local, single-user realization: :func:`register` creates the agent record
and mints an opaque token bound to its ``agent_id``, stored in memory and mirrored best-effort to a
JSON sidecar so it survives a restart.

It is **additive**: a request WITHOUT a credential keeps working exactly as before (the server is
still unauthenticated by default). A credential only tightens things — when one is presented, the
``agent_id`` comes from it and a mismatching body ``agent_id`` is rejected.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import uuid

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

# The header a client presents its credential in (Starlette headers are case-insensitive).
CREDENTIAL_HEADER = "x-tabvis-agent-credential"

_lock = threading.RLock()
_by_token: dict[str, str] = {}   # token -> agent_id
_loaded = False


def _store_path() -> str:
    return os.path.join(get_tabvis_config_home_dir(), "agent-credentials.json")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _by_token.update({str(k): str(v) for k, v in data.items()})
    except (OSError, ValueError):
        pass


def _save() -> None:
    try:
        path = _store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_by_token, fh)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log_for_debugging(f"[CRED] failed to persist credentials: {e}")


def register(
    *, cwd: str = "", model: str | None = None, profile: str | None = None
) -> dict[str, str]:
    """Register a new agent and mint its credential. Returns ``{agent_id, session_id, credential}``.

    The agent record is created here (empty prompt), so a later ``POST /agent`` presenting the
    credential is picked up by the normal reuse path.
    """
    from tabvis.agent.agents import registry

    with _lock:
        _load()
        agent_id = registry.new_agent_id()
        session_id = str(uuid.uuid4())
        registry.create(
            agent_id=agent_id, session_id=session_id, prompt="", model=model, profile=profile, cwd=cwd
        )
        token = "cred_" + secrets.token_urlsafe(24)
        _by_token[token] = agent_id
        _save()
        return {"agent_id": agent_id, "session_id": session_id, "credential": token}


def resolve_agent_id(credential: str | None) -> str | None:
    """The ``agent_id`` a credential is bound to, or None if the credential is unknown/absent."""
    if not credential:
        return None
    with _lock:
        _load()
        return _by_token.get(credential)


def agent_id_from_request_headers(headers: object) -> str | None:
    """Read + resolve the credential header from a request's headers (case-insensitive)."""
    credential = None
    try:
        credential = headers.get(CREDENTIAL_HEADER)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        credential = None
    return resolve_agent_id(credential)
