"""Phase 6 — secrets, credential injection, rich observation & workspace persistence (ROADMAP.md).

IDP-6 (SecretStore + identity refs), IDP-7 (credential injection), OBS-6 (Playwright producers),
OBS-7 (replay retention), WS-7 (workspace re-attach). All additive/gated. ``config_home`` (autouse)
roots the secret file / DB in a tmp dir.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

import tabvis.browser.event_bus as eb
import tabvis.browser.observation as obs
from tabvis.browser import (
    identity_store,
    observation_adapters as adapters,
    secret_store,
    workspace as ws,
)
from tabvis.browser.events import ObservationType, RuntimeEvent
from tabvis.browser.persistence import db


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _reset()
    yield
    _reset()


def _reset() -> None:
    identity_store._cache.clear()
    ws._by_id.clear()
    ws._id_by_agent.clear()
    ws._persisted_loaded = False
    eb._bus = None
    obs._installed = False
    obs._timeline.clear()
    db.close()


# --------------------------------------------------------------------------- IDP-6: secret store


def test_secret_store_round_trip() -> None:
    ref = secret_store.put("hunter2")
    assert ref.startswith("sec_")
    assert secret_store.get(ref) == "hunter2"
    assert secret_store.get("sec_bogus") is None
    secret_store.delete(ref)
    assert secret_store.get(ref) is None


def test_identity_store_credential_and_proxy_refs() -> None:
    ref = identity_store.store_credential("ag_sec", "s3cret")
    identity = identity_store.get_by_agent("ag_sec")
    assert ref in identity.auth.credential_refs
    with pytest.warns(DeprecationWarning):
        # resolve_credential returns plaintext in the agent process; it is the legacy IDP-7 path and is
        # now a deprecated internal interface (CREDENTIAL_INJECTION_DESIGN.md §3.2 gap #1, §15 Phase 0).
        assert identity_store.resolve_credential(ref) == "s3cret"

    proxy_ref = identity_store.set_proxy("ag_sec", "http://proxy.local:8080")
    assert identity_store.get_by_agent("ag_sec").network.proxy_ref == proxy_ref
    # IDP-5 overlay resolves the proxy_ref back to the URL (secret indirection is transparent there).
    assert identity_store.launch_overlay("ag_sec")["proxy"] == "http://proxy.local:8080"


def test_identity_storage_state_round_trip() -> None:
    identity_store.store_storage_state("ag_ss", {"cookies": [{"name": "sid", "value": "abc"}]})
    loaded = identity_store.load_storage_state("ag_ss")
    assert loaded == {"cookies": [{"name": "sid", "value": "abc"}]}
    assert identity_store.get_by_agent("ag_ss").auth.storage_state_ref is not None


# --------------------------------------------------------------------------- IDP-7: credential injection
#
# CREDENTIAL_INJECTION_DESIGN.md §15 Phase 0: no automatic login yet, but the new authentication surface
# MUST NOT be able to receive or emit a plaintext secret. These are failure-first security assertions
# for the boundary; the full flow lands in later phases.


def test_credential_injection_agent_schema_has_no_secret_fields() -> None:
    from tabvis.agent.tools.browser_authenticate_tool import BrowserAuthenticateInput

    fields = set(BrowserAuthenticateInput.model_fields)
    assert fields == {"credential_profile_id"}                      # §16.4: only the profile id
    forbidden = {"username", "password", "totp", "secret_ref", "cookie", "text", "storage_state"}
    assert not (fields & forbidden)


def test_credential_injection_agent_request_rejects_smuggled_secret() -> None:
    from pydantic import ValidationError

    from tabvis.authentication.models import AgentAuthenticationRequest

    AgentAuthenticationRequest(credential_profile_id="work_sso")    # the only accepted shape
    with pytest.raises(ValidationError):
        AgentAuthenticationRequest(credential_profile_id="work_sso", password="hunter2")


def test_credential_injection_resolved_secret_never_stringifies() -> None:
    from tabvis.authentication.secrets import SecretLeakError, secret_from_str

    secret = secret_from_str("hunter2")
    with pytest.raises(SecretLeakError):
        str(secret)
    with pytest.raises(SecretLeakError):
        _ = f"{secret}"
    assert "hunter2" not in repr(secret)
    # the sanctioned path still works, then scrubs
    assert bytes(secret.borrow_bytes()) == b"hunter2"
    secret.release()
    with pytest.raises(SecretLeakError):
        secret.borrow_bytes()


# --------------------------------------------------------------------------- OBS-6 / OBS-7


def test_build_observation_shape() -> None:
    event = adapters.build_observation(
        ObservationType.ARTIFACT_DOWNLOADED, {"url": "https://a.com/f.pdf"}, agent_id="ag", session_id="s"
    )
    assert event.type == "artifact.downloaded" and event.source == "playwright"
    assert event.agent_id == "ag" and event.payload["url"] == "https://a.com/f.pdf"


class _FakePage:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on(self, name: str, _handler: Any) -> None:
        self.events.append(name)


def test_attach_page_producers_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    adapters.attach_page_producers(page, agent_id="ag")  # bus off → no listeners
    assert page.events == []

    monkeypatch.setenv("TABVIS_BROWSER_EVENT_BUS", "1")
    page2 = _FakePage()
    adapters.attach_page_producers(page2, agent_id="ag")
    assert set(page2.events) == {"download", "console"}


def test_emit_observation_appends_timeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_EVENT_BUS", "1")
    event = RuntimeEvent(type=ObservationType.ARTIFACT_DOWNLOADED, source="playwright", agent_id="ag_dl",
                         payload={"url": "https://a.com/f.pdf"})
    asyncio.run(obs.emit_observation(event))
    timeline = obs.get_timeline("ag_dl")
    assert len(timeline) == 1 and timeline[0]["type"] == "artifact.downloaded"


def test_replay_retention_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert obs.is_replay_enabled() is False
    obs._append_timeline(RuntimeEvent(type=ObservationType.PAGE_LOADED, source="runtime", agent_id="ag_r"))
    assert obs.persist_timeline("ag_r") is None                    # replay off → nothing persisted
    monkeypatch.setenv("TABVIS_BROWSER_REPLAY", "1")
    path = obs.persist_timeline("ag_r", session_id="sess-replay")
    assert path is not None and path.endswith("replay.json") and os.path.exists(path)


# --------------------------------------------------------------------------- WS-7: re-attach


def test_workspace_reattaches_from_sqlite() -> None:
    rec = ws.register_workspace(agent_id="ag_ws7", user_data_dir="/tmp/p", session_id="s", identity_id="id_x")
    rec.goal = "persisted goal"
    ws._mirror(rec)  # ensure the mirror has the goal

    # Simulate a restart: drop in-memory state but keep the SQLite mirror.
    ws._by_id.clear()
    ws._id_by_agent.clear()
    ws._persisted_loaded = False

    reattached = ws.get_workspace_for_agent("ag_ws7")
    assert reattached is not None
    assert reattached.workspace_id == rec.workspace_id            # same id restored
    assert reattached.goal == "persisted goal" and reattached.identity_id == "id_x"
