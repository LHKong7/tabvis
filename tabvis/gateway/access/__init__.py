"""Access layer — transports in, protocol frames out (design §3.1 Access Layer, §9).

Parses HTTP/SSE into protocol Commands, dispatches through the router, and projects domain Events into
transport frames. It converts input and output only: it MUST NOT call model or browser tools directly
(design §3.1). This is the standalone gateway app; migrating the existing ``browser/server.py`` routes
onto it is a later, test-guarded step (design §14, §15 Phase 3).
"""

from __future__ import annotations
