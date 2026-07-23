"""Browser data clearing (issue #4): profile-delete safety guards + origin-level clear.

``config_home`` (autouse from tests/conftest) roots the managed root in a tmp dir, so a profile made
under it is genuinely "managed" and the deletion path can be exercised for real.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from tabvis.browser import data_clearing as dc
from tabvis.utils.env_utils import get_tabvis_config_home_dir


# --------------------------------------------------------------------------- profile guards


def _make_profile(name: str) -> str:
    path = os.path.join(get_tabvis_config_home_dir(), name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "Cookies"), "w") as fh:
        fh.write("sid=abc")
    return path


def test_clear_profile_removes_managed_dir() -> None:
    path = _make_profile("browser-test")
    assert os.path.isdir(path)
    result = dc.clear_profile(path, agent_id="ag1", wait=True)  # wait => purge synchronously
    assert result["cleared"] is True
    assert not os.path.exists(path)


def test_clear_profile_refuses_outside_managed_root(tmp_path: Any) -> None:
    outside = tmp_path / "somewhere" / "profile"
    outside.mkdir(parents=True)
    with pytest.raises(dc.DataClearingError, match="not inside the Tabvis-managed root"):
        dc.clear_profile(str(outside))
    assert outside.exists()  # untouched


def test_clear_profile_refuses_managed_root_itself() -> None:
    with pytest.raises(dc.DataClearingError, match="config root itself"):
        dc.clear_profile(get_tabvis_config_home_dir())


def test_clear_profile_refuses_when_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make_profile("browser-busy")
    monkeypatch.setattr(dc, "_active_holder", lambda _p: "ag_owner")
    with pytest.raises(dc.DataClearingError, match="in use by agent"):
        dc.clear_profile(path)
    assert os.path.isdir(path)  # not deleted


def test_clear_profile_noop_when_missing() -> None:
    ghost = os.path.join(get_tabvis_config_home_dir(), "browser-ghost")
    result = dc.clear_profile(ghost)
    assert result["cleared"] is False


def test_clear_profile_writes_audit() -> None:
    path = _make_profile("browser-audit")
    dc.clear_profile(path, agent_id="agX", reason="account switch", wait=True)
    log = dc._audit_log_path()
    assert os.path.exists(log)
    assert "profile_cleared" in open(log, encoding="utf-8").read()


# --------------------------------------------------------------------------- origin clear


class _FakeCDPSession:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict) -> None:
        self.sent.append((method, params))


class _FakeContext:
    def __init__(self) -> None:
        self.cleared_domains: list[str | None] = []
        self.pages = [object()]
        self.cdp = _FakeCDPSession()

    async def clear_cookies(self, domain: str | None = None) -> None:
        self.cleared_domains.append(domain)

    async def new_cdp_session(self, _page: Any) -> _FakeCDPSession:
        return self.cdp


def test_clear_origin_data_clears_cookies_and_storage() -> None:
    ctx = _FakeContext()
    result = asyncio.run(dc.clear_origin_data(ctx, "https://example.com"))
    assert ctx.cleared_domains == ["example.com"]
    assert ctx.cdp.sent[0][0] == "Storage.clearDataForOrigin"
    assert ctx.cdp.sent[0][1]["origin"] == "https://example.com"
    assert "cookies" in result["cleared"] and "indexeddb" in result["cleared"]


def test_clear_origin_data_rejects_bad_origin() -> None:
    with pytest.raises(dc.DataClearingError, match="invalid origin"):
        asyncio.run(dc.clear_origin_data(_FakeContext(), "example.com"))
