"""Gateway metadata store — the authoritative durable layer (design §12).

Unlike ``tabvis/browser/persistence/db.py`` — a deliberately *best-effort shadow* whose failures are
logged and swallowed because JSON remains the source of truth — the gateway store is **authoritative**
(design §12.1). Durable events and the outbox are the record of what happened; a write failure here
is a real error that must surface (and, at the gateway level, make the process unready — design §2.3),
never be silently dropped. So this store lives in its own ``gateway.db`` and does not reuse the shadow's
swallowing wrapper.
"""

from __future__ import annotations
