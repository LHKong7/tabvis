"""Persistence service — the mediating write seam for Browser OS state (PERS-1).

``design.md`` calls for a single Persistence Service that owns all transaction boundaries so no
Context writes files or a database directly. This package is the additive seam toward that: a
``PersistenceService`` facade plus the ``browser-os-data/`` path helpers the design's storage layout
is rooted at. Today the facade is a pure pass-through — its methods delegate to the existing
best-effort writers (``session.write_browser_session``, ``artifacts.record_browser_artifact``,
``registry.persist``) with no change to on-disk format or location — so nothing about how bytes hit
disk changes. Later steps route writes through it and add the SQLite ``runtime.db`` (PERS-2), the
snapshot/working-copy profile cycle (PERS-5), and the two-phase commit (PERS-7).
"""

from __future__ import annotations

from tabvis.browser.persistence.paths import (
    browser_os_data_subdir,
    get_browser_os_data_dir,
    identities_dir,
    logs_dir,
    runtime_db_path,
    sessions_dir,
    workspaces_dir,
)
from tabvis.browser.persistence.service import PersistenceService, get_persistence_service

__all__ = [
    "PersistenceService",
    "get_persistence_service",
    "get_browser_os_data_dir",
    "browser_os_data_subdir",
    "identities_dir",
    "workspaces_dir",
    "sessions_dir",
    "logs_dir",
    "runtime_db_path",
]
