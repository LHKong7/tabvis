"""Compact-warning suppression state.

Tracks whether the "context left until autocompact" warning should be suppressed. The warning is
suppressed immediately after successful compaction since accurate token counts aren't available
until the next API response.
"""

from __future__ import annotations

from tabvis.state.store import create_store

# Tracks whether the compact warning should be suppressed.
compact_warning_store = create_store(False)


def suppress_compact_warning() -> None:
    """Suppress the compact warning. Call after successful compaction."""
    compact_warning_store.set_state(lambda _prev: True)


def clear_compact_warning_suppression() -> None:
    """Clear the compact warning suppression. Called at start of new compact attempt."""
    compact_warning_store.set_state(lambda _prev: False)
