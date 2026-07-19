"""Startup profiler

The original records named checkpoints with high-resolution timestamps for startup
performance analysis. The full timing implementation is planned for a later implementation phase; for now
this is a no-op so the entry chain evaluates faithfully.
"""

from __future__ import annotations


def profile_checkpoint(_name: str) -> None:
    return None
