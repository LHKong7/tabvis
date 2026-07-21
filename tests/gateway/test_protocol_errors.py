"""Phase 0 — the stable error catalog (design §9.7)."""

from __future__ import annotations

from tabvis.gateway.protocol.errors import CATALOG, GatewayError, spec_for


def test_catalog_codes_are_self_consistent() -> None:
    for code, spec in CATALOG.items():
        assert spec.code == code
        assert 100 <= spec.http_status <= 599
        assert isinstance(spec.retryable, bool)
        assert spec.message


def test_known_code_pulls_status_and_retryable_from_catalog() -> None:
    err = GatewayError("CAPACITY_EXCEEDED")
    assert err.code == "CAPACITY_EXCEEDED"
    assert err.http_status == 429
    assert err.retryable is True


def test_run_already_active_is_the_documented_example() -> None:
    # design §9.7 shows exactly this code/message/retryable trio.
    err = GatewayError("RUN_ALREADY_ACTIVE", details={"run_id": "run_abc"})
    body = err.to_body()
    assert body == {
        "error": {
            "code": "RUN_ALREADY_ACTIVE",
            "message": "Agent already has an active run",
            "retryable": False,
            "details": {"run_id": "run_abc"},
        }
    }


def test_message_override_keeps_the_code_meaning() -> None:
    err = GatewayError("VALIDATION_FAILED", message="'type' is required")
    assert err.code == "VALIDATION_FAILED"
    assert err.message == "'type' is required"


def test_trace_id_included_only_when_set() -> None:
    assert "trace_id" not in GatewayError("INTERNAL").to_body()["error"]
    assert GatewayError("INTERNAL", trace_id="tr_1").to_body()["error"]["trace_id"] == "tr_1"


def test_unknown_code_falls_back_to_internal() -> None:
    spec = spec_for("NOPE_NOT_REAL")
    assert spec.code == "INTERNAL"
    assert GatewayError("NOPE_NOT_REAL").http_status == 500
