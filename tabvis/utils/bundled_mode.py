"""Bun runtime / bundled-mode detection

The TS module probes the Bun runtime: ``isRunningWithBun`` checks ``process.versions.bun``;
``isInBundledMode`` checks ``Bun.embeddedFiles`` (present only in a Bun-compiled standalone
executable).

Behavior note: the Python implementation does NOT run under Bun — there is no ``process.versions``
and no ``Bun`` global. Both probes therefore return ``False`` here, which is the correct
answer for the CPython runtime (we are never "running with Bun" nor a "Bun-compiled binary").
We still keep best-effort detection hooks so the functions report ``True`` if a future
embedder injects the corresponding markers, but in the normal Python runtime they are ``False``.

Casing: snake_case identifiers.
"""

from __future__ import annotations

import builtins
import sys


def is_running_with_bun() -> bool:
    """Whether the current runtime is Bun.

    Mirrors TS ``process.versions.bun !== undefined``. Under CPython there is no ``Bun``
    runtime, so this is ``False`` unless a ``bun`` marker has been injected into
    ``sys.versions`` (best-effort; normally absent).
    """
    # https://bun.com/guides/util/detect-bun — the TS source reads ``process.versions.bun``.
    versions = getattr(sys, "versions", None)
    if isinstance(versions, dict):
        return versions.get("bun") is not None
    return False


def is_in_bundled_mode() -> bool:
    """Whether running as a Bun-compiled standalone executable.

    Mirrors TS ``typeof Bun !== 'undefined' && Array.isArray(Bun.embeddedFiles) &&
    Bun.embeddedFiles.length > 0``. Under CPython there is no ``Bun`` global, so this is
    ``False`` unless an embedder injects a ``Bun`` object exposing a non-empty
    ``embeddedFiles`` list.
    """
    bun = getattr(builtins, "Bun", None)
    if bun is None:
        return False
    embedded = getattr(bun, "embeddedFiles", None)
    return isinstance(embedded, list) and len(embedded) > 0
