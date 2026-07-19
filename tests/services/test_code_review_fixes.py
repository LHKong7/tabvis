"""Regression tests for the verified code-review fixes (Phase 1-7 hardening)."""

from __future__ import annotations

from typing import Any

import pytest

import tabvis.browser.intents.execution_registry as exreg
from tabvis.browser import identity_store
from tabvis.browser.intents import get_execution_registry
from tabvis.browser.intents.types import ExecutionRecord


@pytest.fixture(autouse=True)
def _clean() -> Any:
    exreg._registry = None
    identity_store._cache.clear()
    yield
    exreg._registry = None
    identity_store._cache.clear()


def test_list_recent_limit_zero_is_empty() -> None:
    reg = get_execution_registry()
    reg.record(ExecutionRecord(execution_id="e1", intent="navigate", status="completed"))
    reg.record(ExecutionRecord(execution_id="e2", intent="navigate", status="completed"))
    assert reg.list_recent(0) == []          # limit=0 → empty (not "all")
    assert len(reg.list_recent(1)) == 1
    assert len(reg.list_recent(None)) == 2
    assert reg.list_recent(-3) == []         # negative clamps to empty


def test_launch_overlay_omits_unresolvable_proxy_secret_ref() -> None:
    identity_store.resolve("ag_px", profile_ref="/tmp/p")
    # A secret_ref that is NOT in the store must NOT be used as a literal proxy server.
    identity_store.update_for_agent("ag_px", {"network": {"proxy_ref": "sec_does_not_exist"}})
    assert "proxy" not in identity_store.launch_overlay("ag_px")


def test_launch_overlay_keeps_raw_proxy_url() -> None:
    identity_store.resolve("ag_px2", profile_ref="/tmp/p")
    identity_store.update_for_agent("ag_px2", {"network": {"proxy_ref": "http://proxy.local:8080"}})
    assert identity_store.launch_overlay("ag_px2")["proxy"] == "http://proxy.local:8080"


def test_launch_overlay_resolves_stored_proxy_secret() -> None:
    # A real stored secret_ref resolves back to the URL transparently.
    ref = identity_store.set_proxy("ag_px3", "http://real.proxy:3128")
    assert ref.startswith("sec_")
    assert identity_store.launch_overlay("ag_px3")["proxy"] == "http://real.proxy:3128"
