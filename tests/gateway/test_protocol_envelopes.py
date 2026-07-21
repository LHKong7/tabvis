"""Phase 0 — command and event envelopes (design §9.2, §9.3, §9.5)."""

from __future__ import annotations

import json

import pytest

from tabvis.gateway import PROTOCOL
from tabvis.gateway.protocol.commands import Command, CommandResult, CommandType
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventEnvelope, EventScope, parse_cursor


# --- commands ----------------------------------------------------------------------------------


def test_command_from_dict_parses_a_valid_envelope() -> None:
    cmd = Command.from_dict(
        {
            "protocol": PROTOCOL,
            "command_id": "cmd_1",
            "type": CommandType.RUN_CREATE,
            "data": {"agent_id": "ag_1"},
        }
    )
    assert cmd.type == "run.create"
    assert cmd.command_id == "cmd_1"
    assert cmd.data == {"agent_id": "ag_1"}


def test_command_from_dict_mints_command_id_when_absent() -> None:
    cmd = Command.from_dict({"type": CommandType.RUN_CANCEL})
    assert cmd.command_id.startswith("cmd_")


def test_command_from_dict_rejects_wrong_protocol() -> None:
    with pytest.raises(GatewayError) as ei:
        Command.from_dict({"protocol": "other.v9", "type": "run.create"})
    assert ei.value.code == "UNSUPPORTED_PROTOCOL"


def test_command_from_dict_requires_type() -> None:
    with pytest.raises(GatewayError) as ei:
        Command.from_dict({"data": {}})
    assert ei.value.code == "VALIDATION_FAILED"


def test_command_round_trips() -> None:
    cmd = Command(type="run.create", data={"x": 1}, command_id="cmd_9")
    assert Command.from_dict(cmd.to_dict()).to_dict() == cmd.to_dict()


def test_command_result_marks_duplicates() -> None:
    res = CommandResult(command_id="cmd_1", duplicate=True, data={"run_id": "run_1"})
    assert res.to_dict()["duplicate"] is True


# --- events ------------------------------------------------------------------------------------


def _envelope(cursor: int = 18472, seq: int = 12) -> EventEnvelope:
    return EventEnvelope(
        event_id="evt_1",
        cursor=cursor,
        aggregate_type="run",
        aggregate_id="run_1",
        seq=seq,
        type="tool.completed",
        scope=EventScope(agent_id="ag_1", run_id="run_1"),
        data={"k": "v"},
        correlation_id="cmd_1",
    )


def test_event_to_dict_matches_the_wire_shape() -> None:
    d = _envelope().to_dict()
    assert d["protocol"] == PROTOCOL
    assert d["aggregate"] == {"type": "run", "id": "run_1"}
    assert d["seq"] == 12
    assert d["type"] == "tool.completed"
    assert d["scope"]["agent_id"] == "ag_1"
    assert d["cursor"] == "0000000000018472"  # zero-padded, 16 wide (design §9.5)


def test_event_scope_drops_none_fields() -> None:
    scope = EventScope(agent_id="ag_1").to_dict()
    assert scope == {"tenant_id": "local", "agent_id": "ag_1"}


def test_sse_frame_uses_cursor_as_event_id() -> None:
    frame = _envelope().to_sse_frame()
    assert frame.startswith("id: 0000000000018472\n")
    assert "event: tool.completed\n" in frame
    body_line = [ln for ln in frame.splitlines() if ln.startswith("data: ")][0]
    payload = json.loads(body_line[len("data: ") :])
    assert payload["event_id"] == "evt_1"


def test_parse_cursor_handles_padded_plain_and_empty() -> None:
    assert parse_cursor("0000000000018472") == 18472
    assert parse_cursor(42) == 42
    assert parse_cursor(None) == 0
    assert parse_cursor("") == 0
