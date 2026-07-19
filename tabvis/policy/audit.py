"""Audit event + emission for a policy decision (PP-1 shape, PP-6 emission).

``docs/permission-policy-engine_v1.md`` §2 & §10: every allow/deny/ask must be reconstructable from an
audit trail via ``request_id`` / ``execution_id``, and **no secret ever enters the record**. This
module defines the event shape (:class:`PolicyDecisionEvent`) and a synchronous, pluggable emitter
(:func:`emit`) with a default structured-log sink.

The emitter is synchronous because the policy decision runs on the tool's sync ``check_permissions``
path; it mirrors the async EventBus's isolate-failing-sink contract but never needs a running loop. A
later step can register a sink that bridges onto the EventBus.

Redaction: only the resource *reference* is recorded, and a ``url:`` resource is stripped of its query
and fragment (which may carry ``?token=…``) and run through ``scrub_secrets`` before it leaves this
module. A ``secret:`` resource is a ref id, never a value.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

_logger = logging.getLogger("tabvis.policy.audit")

# Audit is on unless explicitly disabled — the trail is a security control, not a debug aid.
_AUDIT_ENV = "TABVIS_PERMISSION_AUDIT"
_FALSY = frozenset({"0", "false", "no", "off"})


def is_audit_enabled() -> bool:
    val = os.environ.get(_AUDIT_ENV)
    return not (val is not None and val.strip().lower() in _FALSY)


@dataclass(frozen=True)
class PolicyDecisionEvent:
    """A ``policy.decision`` audit record. Serialize with :meth:`to_dict`."""

    effect: str  # allow | deny | ask
    action: str
    resource: str
    mode: str
    rule_id: str | None = None
    reason: str = ""
    # Correlation ids — all optional; populate whatever the calling context has.
    request_id: str | None = None
    execution_id: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None
    event: str = field(default="policy.decision")

    def to_dict(self) -> dict[str, Any]:
        """Flat dict for logging / the event bus, omitting unset correlation ids."""
        out: dict[str, Any] = {
            "event": self.event,
            "effect": self.effect,
            "action": self.action,
            "resource": self.resource,
            "mode": self.mode,
            "rule_id": self.rule_id,
            "reason": self.reason,
        }
        for key in (
            "request_id",
            "execution_id",
            "session_id",
            "workspace_id",
            "agent_id",
        ):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        return out


def _redact_resource(resource: str) -> str:
    """Strip a ``url:`` resource's query/fragment and any inline credentials before it is recorded."""
    from tabvis.utils.browser_config import scrub_secrets

    red = resource
    if resource.startswith("url:"):
        for sep in ("?", "#"):
            idx = red.find(sep)
            if idx != -1:
                red = red[:idx]
    return scrub_secrets(red)


# --- synchronous, pluggable sink registry (PP-6) --------------------------------------------------

AuditSink = Callable[[dict[str, Any]], None]


def _default_log_sink(record: dict[str, Any]) -> None:
    """Emit the audit record as a single structured JSON line at INFO."""
    _logger.info("policy.decision %s", json.dumps(record, sort_keys=True))


_sinks: list[AuditSink] = [_default_log_sink]


def register_sink(sink: AuditSink) -> Callable[[], None]:
    """Add an audit sink; returns an unsubscribe callable. Sinks receive the redacted record dict."""
    _sinks.append(sink)

    def _unsubscribe() -> None:
        try:
            _sinks.remove(sink)
        except ValueError:
            pass

    return _unsubscribe


def emit(event: PolicyDecisionEvent) -> None:
    """Redact + deliver an audit event to every sink. No-op when disabled; never raises."""
    if not is_audit_enabled():
        return
    record = event.to_dict()
    record["resource"] = _redact_resource(record.get("resource", ""))
    for sink in list(_sinks):
        try:
            sink(record)
        except Exception:  # noqa: BLE001 - one bad sink must not break the decision path
            _logger.exception("policy audit sink failed")
