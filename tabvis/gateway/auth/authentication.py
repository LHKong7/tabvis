"""Resolve a request into a Principal (design §3.1).

This delegates to ``tabvis/browser/server_auth.py`` so the gateway's posture is identical to the
existing server: on a loopback bind an unauthenticated request is the local admin; a non-loopback bind
requires an admin bearer token (or a per-agent credential), and an unauthenticated request there is
rejected with ``UNAUTHENTICATED``. The policy-layer principal returned by ``server_auth`` is mapped to
the gateway's richer :class:`Principal`.
"""

from __future__ import annotations

from typing import Any

from tabvis.browser import server_auth
from tabvis.gateway.auth.principals import Principal, agent_principal, local_admin
from tabvis.gateway.protocol.errors import GatewayError


def resolve_principal(headers: Any, *, host: str | None) -> Principal:
    """Resolve the request Principal or raise ``UNAUTHENTICATED``.

    ``headers`` is any mapping with a case-insensitive ``.get`` (Starlette ``request.headers`` works).
    ``host`` is the bound host — loopback opens the management face, as today.
    """
    auth_required = server_auth.auth_required_for_host(host)
    policy_principal = server_auth.resolve_principal(headers, auth_required=auth_required)
    if policy_principal is None:
        raise GatewayError("UNAUTHENTICATED")
    if getattr(policy_principal, "is_admin", False):
        return local_admin()
    agent_id = getattr(policy_principal, "agent_id", None)
    if agent_id:
        return agent_principal(agent_id)
    # server_auth only returns admin or agent principals; anything else is unexpected.
    raise GatewayError("UNAUTHENTICATED")
