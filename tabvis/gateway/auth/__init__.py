"""Gateway authentication and authorization (design §3.1, §13).

Every command carries a resolved :class:`~tabvis.gateway.auth.principals.Principal`, and body fields
never override the identity established by credentials (design §3.1). This layer wraps the existing
``tabvis/browser/server_auth.py`` resolution — loopback → local admin, admin bearer token → admin,
per-agent credential → that agent — so the gateway stays byte-for-byte faithful to today's posture
while exposing the design's richer Principal shape.
"""

from __future__ import annotations
