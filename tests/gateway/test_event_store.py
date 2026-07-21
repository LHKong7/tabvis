"""Phase 1 — the durable EventStore: cursors, per-aggregate seq, replay (design §5.3, §5.5)."""

from __future__ import annotations

from tabvis.gateway.events.store import EventStore
from tabvis.gateway.events.subscriptions import get_live_bus
from tabvis.gateway.protocol.events import AGGREGATE_RUN, EventScope, EventType
from tabvis.gateway.store import db


def test_seq_is_per_aggregate_and_one_based() -> None:
    store = EventStore()
    a1 = store.append(AGGREGATE_RUN, "run_a", EventType.RUN_CREATED)
    a2 = store.append(AGGREGATE_RUN, "run_a", EventType.RUN_STARTED)
    b1 = store.append(AGGREGATE_RUN, "run_b", EventType.RUN_CREATED)
    assert (a1.seq, a2.seq) == (1, 2)
    assert b1.seq == 1  # a different aggregate restarts the sequence


def test_cursor_is_globally_monotonic_across_aggregates() -> None:
    store = EventStore()
    cursors = [
        store.append(AGGREGATE_RUN, f"run_{i % 3}", EventType.RUN_STARTED).cursor for i in range(10)
    ]
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == 10  # strictly increasing, no repeats


def test_read_after_cursor_has_no_gap_or_duplicate() -> None:
    store = EventStore()
    envelopes = [store.append(AGGREGATE_RUN, "run_x", EventType.RUN_STARTED) for _ in range(5)]
    # A subscriber that already saw the first two reconnects at their cursor.
    resume_at = envelopes[1].cursor
    replay = store.read(after_cursor=resume_at)
    assert [e.cursor for e in replay] == [e.cursor for e in envelopes[2:]]


def test_read_filters_by_aggregate() -> None:
    store = EventStore()
    store.append(AGGREGATE_RUN, "run_1", EventType.RUN_STARTED)
    store.append(AGGREGATE_RUN, "run_2", EventType.RUN_STARTED)
    store.append(AGGREGATE_RUN, "run_1", EventType.RUN_COMPLETED)
    only_run_1 = store.read(aggregate_id="run_1")
    assert [e.type for e in only_run_1] == [EventType.RUN_STARTED, EventType.RUN_COMPLETED]


def test_stored_envelope_is_complete_including_its_cursor() -> None:
    store = EventStore()
    appended = store.append(
        AGGREGATE_RUN, "run_1", EventType.RUN_CREATED,
        scope=EventScope(agent_id="ag_1"), data={"k": "v"}, correlation_id="cmd_1",
    )
    (read_back,) = store.read()
    assert read_back.cursor == appended.cursor
    assert read_back.event_id == appended.event_id
    assert read_back.seq == 1
    assert read_back.data == {"k": "v"}
    assert read_back.scope.agent_id == "ag_1"
    assert read_back.correlation_id == "cmd_1"
    # a run event auto-populates scope.run_id from the aggregate id.
    assert read_back.scope.run_id == "run_1"


def test_latest_cursor_tracks_the_head() -> None:
    store = EventStore()
    assert store.latest_cursor() == 0
    last = store.append(AGGREGATE_RUN, "run_1", EventType.RUN_STARTED)
    assert store.latest_cursor() == last.cursor


def test_append_enqueues_outbox_and_notifies_live_bus() -> None:
    seen = []
    get_live_bus().subscribe(seen.append)
    store = EventStore()
    ev = store.append(AGGREGATE_RUN, "run_1", EventType.RUN_STARTED)
    # outbox row created in the same transaction (design §1.5, §5.3).
    pending = db.pending_outbox()
    assert [p["cursor"] for p in pending] == [ev.cursor]
    # live fan-out fired for a self-managed append.
    assert [e.cursor for e in seen] == [ev.cursor]
    # marking delivered drains it from the pending queue.
    db.mark_outbox_delivered(ev.cursor)
    assert db.pending_outbox() == []
