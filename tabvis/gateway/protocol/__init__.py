"""Gateway protocol — stable IDs, envelopes, error catalog, and vocabulary (design §9).

Everything a channel or client needs to speak to the gateway lives here and nowhere else:

* :mod:`tabvis.gateway.protocol.ids`      — typed, prefixed identifiers.
* :mod:`tabvis.gateway.protocol.errors`   — the stable error-code catalog and error body shape.
* :mod:`tabvis.gateway.protocol.events`   — the append-only Event envelope and the event catalog.
* :mod:`tabvis.gateway.protocol.commands` — the Command envelope and command vocabulary.

These are pure data/contract modules: no I/O, no runtime state, safe to import anywhere.
"""

from __future__ import annotations
