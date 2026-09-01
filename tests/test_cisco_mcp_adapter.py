"""Tests for CiscoMCPAdapter."""
import pytest
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter


def _adapter():
    return CiscoMCPAdapter(LoggingAgent())


def test_log_tool_call_success():
    adapter = _adapter()
    adapter.log_tool_call(
        tool_name="get_alerts",
        latency_ms=300.0,
        status="success",
        product=CiscoMCPAdapter.THOUSANDEYES,
        input_data={"agent_id": "1"},
        output_data={"alerts": []},
    )
    state = adapter.get_state()
    assert "thousandeyes.get_alerts" in state["agents"]
    agent = state["agents"]["thousandeyes.get_alerts"]
    assert agent["error_rate"] == 0.0
    assert agent["total_observations"] == 1


def test_log_tool_call_failure():
    adapter = _adapter()
    adapter.log_tool_call(
        tool_name="get_tests",
        latency_ms=0.0,
        status="failed",
        product=CiscoMCPAdapter.THOUSANDEYES,
        error_details={"message": "timeout"},
    )
    state = adapter.get_state()
    agent = state["agents"]["thousandeyes.get_tests"]
    assert agent["error_rate"] == 1.0


def test_agent_id_format():
    adapter = _adapter()
    adapter.log_tool_call(
        tool_name="send_message",
        latency_ms=120.0,
        status="success",
        product=CiscoMCPAdapter.WEBEX,
    )
    state = adapter.get_state()
    assert "webex.send_message" in state["agents"]


def test_product_uppercased_in_input_normalised():
    adapter = _adapter()
    adapter.log_tool_call(
        tool_name="get_devices",
        latency_ms=200.0,
        status="success",
        product="Catalyst_Center",
    )
    state = adapter.get_state()
    assert "catalyst_center.get_devices" in state["agents"]


def test_multiple_products_tracked_independently():
    adapter = _adapter()
    adapter.log_tool_call("get_alerts", 100.0, "success", CiscoMCPAdapter.THOUSANDEYES)
    adapter.log_tool_call("list_rooms", 150.0, "success", CiscoMCPAdapter.WEBEX)
    state = adapter.get_state()
    assert "thousandeyes.get_alerts" in state["agents"]
    assert "webex.list_rooms" in state["agents"]


def test_latency_alarm_trips_on_spike():
    adapter = _adapter()
    # Warm baseline: 10 fast calls.
    for _ in range(10):
        adapter.log_tool_call("get_alerts", 100.0, "success", CiscoMCPAdapter.THOUSANDEYES)
    # Spike: next call is 10x baseline.
    adapter.log_tool_call("get_alerts", 1000.0, "success", CiscoMCPAdapter.THOUSANDEYES)
    state = adapter.get_state()
    alarm_names = [a["rule"] for a in state["anomalies"]]
    assert "latency_high" in alarm_names


def test_error_rate_alarm_trips():
    adapter = _adapter()
    for _ in range(5):
        adapter.log_tool_call("get_tests", 100.0, "failed", CiscoMCPAdapter.THOUSANDEYES)
    state = adapter.get_state()
    alarm_names = [a["rule"] for a in state["anomalies"]]
    assert "error_rate_high" in alarm_names


def test_ingest_session_log_basic():
    adapter = _adapter()
    log = {
        "tool_calls": [
            {"tool": "get_alerts", "product": "thousandeyes", "latency_ms": 200, "status": "success"},
            {"tool": "list_rooms",  "product": "webex",        "latency_ms": 150, "status": "success"},
            {"tool": "get_devices", "product": "catalyst_center", "latency_ms": 300, "status": "failed",
             "error": "connection refused"},
        ]
    }
    state = adapter.ingest_session_log(log)
    assert "thousandeyes.get_alerts" in state["agents"]
    assert "webex.list_rooms" in state["agents"]
    assert "catalyst_center.get_devices" in state["agents"]
    assert state["agents"]["catalyst_center.get_devices"]["error_rate"] == 1.0


def test_ingest_session_log_empty():
    adapter = _adapter()
    state = adapter.ingest_session_log({})
    assert state["agents"] == {}
    assert state["anomalies"] == []


def test_ingest_session_log_null_tool_calls():
    adapter = _adapter()
    state = adapter.ingest_session_log({"tool_calls": None})
    assert state["agents"] == {}


def test_wrap_agent_passthrough():
    adapter = _adapter()
    sentinel = object()
    assert adapter.wrap_agent(sentinel) is sentinel


def test_known_products_constant():
    assert CiscoMCPAdapter.THOUSANDEYES in CiscoMCPAdapter.KNOWN_PRODUCTS
    assert CiscoMCPAdapter.WEBEX in CiscoMCPAdapter.KNOWN_PRODUCTS
    assert CiscoMCPAdapter.NEXUS_DASHBOARD in CiscoMCPAdapter.KNOWN_PRODUCTS
