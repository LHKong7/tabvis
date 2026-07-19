"""Random-UUID indirection

The TS file exists solely as an indirection point for the package.json ``browser``
field (swapped for ``crypto.browser.ts`` under a Bun browser build to avoid inlining a
~500KB crypto-browserify polyfill). It re-exports a single binding: ``randomUUID`` from
Node's ``crypto``.

The browser-build indirection has no Python analogue, so this module collapses to the
one exported binding. ``random_uuid`` returns a canonical RFC 4122 v4 UUID string
(8-4-4-4-12 lowercase hex) — the same shape Node's ``crypto.randomUUID`` produces.

Implementation note: the module name ``tabvis.utils.crypto`` does NOT shadow the stdlib
``crypto`` (there is none) nor the stdlib ``uuid`` — ``import uuid`` below resolves to
the stdlib under Python 3 absolute imports, not to the sibling ``tabvis.utils.uuid``.
"""

from __future__ import annotations

import uuid as _stdlib_uuid


def random_uuid() -> str:
    """Generate a cryptographically-random RFC 4122 v4 UUID string.

    Parity with Node ``crypto.randomUUID``: a canonical 8-4-4-4-12 lowercase-hex UUID.
    :func:`uuid.uuid4` draws from ``os.urandom`` (CSPRNG), matching the security
    posture of the TS source.
    """
    return str(_stdlib_uuid.uuid4())
