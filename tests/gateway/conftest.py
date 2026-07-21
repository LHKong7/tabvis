"""Reset the gateway's process-global singletons between tests.

The root ``conftest`` already pins ``TABVIS_CONFIG_DIR`` at a fresh tmp dir per test, so each test gets
its own ``gateway.db``. But the store connection, the ``EventStore`` / ``RunStore`` / ``LiveBus``
singletons, and the SQLite handle are module globals that would otherwise leak across tests — this
autouse fixture drops them so every test starts cold.
"""

from __future__ import annotations

import pytest

from tabvis.gateway.events import store as event_store_mod
from tabvis.gateway.events import subscriptions
from tabvis.gateway.runtime import run_store as run_store_mod
from tabvis.gateway.store import db


@pytest.fixture(autouse=True)
def _reset_gateway_globals():
    _reset()
    yield
    _reset()


def _reset() -> None:
    db.close()
    event_store_mod._store = None
    run_store_mod._run_store = None
    subscriptions.reset_live_bus()
