"""Profile generation tracking (Resume Plus §6.2).

When an agent's persistent Chromium profile is intentionally cleared/reset, the profile directory is
recreated empty but the agent's durable binding is unchanged. Without a marker, the next Resume would
see a logged-out profile and could not tell an *intentional reset* from an *unexpected missing
profile*. This tiny per-agent counter records that intent: a clear bumps the generation, and the
resolver can report ``new_profile`` recovery when it sees a bump it has not accounted for.

Storage: ``<config-home>/browser-profile-generations/<agent>.json`` — a JSON record, mode ``0600``.
Pure filesystem, no dependency on the browser manager, so it is safe to read/write from anywhere.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_DIRNAME = "browser-profile-generations"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(agent_id: str) -> str:
    s = _SAFE.sub("-", (agent_id or "").strip()).strip(".-")
    return s[:128] or "agent"


def _dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), _DIRNAME)


def _path(agent_id: str) -> str:
    return os.path.join(_dir(), f"{_slug(agent_id)}.json")


@dataclass
class ProfileGeneration:
    agent_id: str
    generation: int = 0
    reset_at: str | None = None
    reason: str = ""
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def info(agent_id: str) -> ProfileGeneration:
    """The agent's current profile generation record (generation 0 if never reset)."""
    try:
        with open(_path(agent_id), encoding="utf-8") as fh:
            d = json.load(fh)
        return ProfileGeneration(
            agent_id=agent_id, generation=int(d.get("generation", 0)),
            reset_at=d.get("reset_at"), reason=d.get("reason", ""),
            history=list(d.get("history", [])),
        )
    except (OSError, ValueError, TypeError):
        return ProfileGeneration(agent_id=agent_id)


def current(agent_id: str) -> int:
    return info(agent_id).generation


def bump(agent_id: str, *, reason: str = "") -> ProfileGeneration:
    """Record an intentional profile reset; returns the new record with an incremented generation."""
    rec = info(agent_id)
    rec.generation += 1
    rec.reset_at = _utc_now()
    rec.reason = reason
    rec.history.append({"generation": str(rec.generation), "reset_at": rec.reset_at, "reason": reason})
    rec.history = rec.history[-20:]
    _save(rec)
    return rec


def _save(rec: ProfileGeneration) -> None:
    try:
        d = _dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = _path(rec.agent_id)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec.to_dict(), fh, indent=2, default=str)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as e:
        log_for_debugging(f"[PROFILE-GEN] failed to persist {rec.agent_id}: {e}")
