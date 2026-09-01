"""Tests for CiscoMCPAdapter."""
import pytest

from agent_logging_system import LoggingAgent
from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter
from agent_logging_system.observation import LATENCY_GENERATION, LATENCY_MACHINE


@pytest.fixture
def monitor():
    return LoggingAgent()


@pytest.fixture
def adapter(monitor):
    return CiscoMCPAdapter(monitor)


class TestLogToolCall:
    def test_success_creates_observation(self, adapter, monitor):
        adapter.log_tool_call(
            tool_name="get_test_results",
            product=CiscoMCPAdapter.THOUSANDEYES,
            latency_ms=250,
            status="success",
            input_data={"test_id": "42"},
            output_data={"results": [{"loss": 0}]},
        )
        state = monitor.get_system_state()
        assert "thousandeyes.get_test_results" in state["agents"]

    def test_agent_id_is_product_dot_tool(self, adapter, monitor):
        adapter.log_tool_call(
            tool_name="send_message",
            product=CiscoMCPAdapter.WEBEX,
            latency_ms=80,
        )
        state = monitor.get_system_state()
        assert "webex.send_message" in state["agents"]

    def test_error_status_maps_to_failed(self, adapter, monitor):
        adapter.log_tool_call(
            tool_name="get_devices",
            product=CiscoMCPAdapter.CATALYST_CENTER,
            latency_ms=500,
            status="error",
            error_details={"code": "500", "message": "Internal Server Error"},
        )
        state = monitor.get_system_state()
        agent = state["agents"]["catalyst_center.get_devices"]
        assert agent["error_rate"] > 0

    def test_timeout_status_maps_to_timeout(self, adapter, monitor):
        adapter.log_tool_call(
            tool_name="get_fabric_health",
            product=CiscoMCPAdapter.NEXUS_DASHBOARD,
            latency_ms=30000,
            status="timeout",
        )
        state = monitor.get_system_state()
        agent = state["agents"]["nexus_dashboard.get_fabric_health"]
        assert agent["error_rate"] > 0

    def test_ok_status_maps_to_success(self, adapter, monitor):
        adapter.log_tool_call(
            tool_name="get_tunnels",
            product=CiscoMCPAdapter.CATALYST_SDWAN,
            latency_ms=120,
            status="ok",
        )
        state = monitor.get_system_state()
        agent = state["agents"]["catalyst_sdwan.get_tunnels"]
        assert agent["error_rate"] == 0.0

    def test_unknown_status_maps_to_failed(self, adapter, monitor):
        adapter.log_tool_call(
            tool_name="show_version",
            product=CiscoMCPAdapter.IOS_XE,
            latency_ms=90,
            status="unexpected_value",
        )
        state = monitor.get_system_state()
        agent = state["agents"]["ios_xe.show_version"]
        assert agent["error_rate"] > 0

    def test_latency_kind_default_is_machine(self, adapter, monitor):
        adapter.log_tool_call(
            tool_name="get_agents",
            product=CiscoMCPAdapter.THOUSANDEYES,
            latency_ms=300,
        )
        state = monitor.get_system_state()
        agent = state["agents"]["thousandeyes.get_agents"]
        assert agent["machine_observations"] == 1
        assert agent["generation_observations"] == 0

    def test_generation_latency_kind_does_not_alarm(self, adapter, monitor):
        for _ in range(10):
            adapter.log_tool_call(
                tool_name="stream_events",
                product=CiscoMCPAdapter.WEBEX,
                latency_ms=60000,
                latency_kind=LATENCY_GENERATION,
            )
        state = monitor.get_system_state()
        latency_alarms = [a for a in state["anomalies"] if a["rule"] == "latency_high"]
        assert len(latency_alarms) == 0

    def test_separate_products_tracked_independently(self, adapter, monitor):
        adapter.log_tool_call("get_alerts", CiscoMCPAdapter.THOUSANDEYES, 200)
        adapter.log_tool_call("get_alerts", CiscoMCPAdapter.WEBEX, 300)
        state = monitor.get_system_state()
        assert "thousandeyes.get_alerts" in state["agents"]
        assert "webex.get_alerts" in state["agents"]
        assert (
            state["agents"]["thousandeyes.get_alerts"]
            is not state["agents"]["webex.get_alerts"]
        )

    def test_wrap_agent_requires_product(self, monitor):
        adapter = CiscoMCPAdapter(monitor)  # no product set
        with pytest.raises(ValueError, match="product"):
            adapter.wrap_agent(object())

    def test_wrap_agent_intercepts_sync_call_tool(self, monitor):
        class FakeMCPSession:
            def call_tool(self, name, arguments=None):
                return type("R", (), {"isError": False, "content": []})()

        adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.THOUSANDEYES)
        wrapped = adapter.wrap_agent(FakeMCPSession())
        wrapped.call_tool("get_test_results", {"test_id": "1"})

        state = monitor.get_system_state()
        assert "thousandeyes.get_test_results" in state["agents"]

    def test_wrap_agent_error_result_counts_as_failed(self, monitor):
        class FakeMCPSession:
            def call_tool(self, name, arguments=None):
                return type("R", (), {"isError": True, "content": []})()

        adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.WEBEX)
        wrapped = adapter.wrap_agent(FakeMCPSession())
        wrapped.call_tool("send_message", {})

        state = monitor.get_system_state()
        assert state["agents"]["webex.send_message"]["error_rate"] > 0

    def test_wrap_agent_exception_counts_as_failed(self, monitor):
        class FakeMCPSession:
            def call_tool(self, name, arguments=None):
                raise ConnectionError("timeout")

        adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.CATALYST_CENTER)
        wrapped = adapter.wrap_agent(FakeMCPSession())
        with pytest.raises(ConnectionError):
            wrapped.call_tool("get_devices", {})

        state = monitor.get_system_state()
        assert state["agents"]["catalyst_center.get_devices"]["error_rate"] > 0

    def test_wrap_agent_passthrough_non_call_tool_attrs(self, monitor):
        class FakeMCPSession:
            server_name = "thousandeyes-mcp"
            def call_tool(self, *a, **kw): ...

        adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.THOUSANDEYES)
        wrapped = adapter.wrap_agent(FakeMCPSession())
        assert wrapped.server_name == "thousandeyes-mcp"


class TestIngestSessionLog:
    def test_basic_ingest(self, adapter, monitor):
        log = {
            "product": CiscoMCPAdapter.THOUSANDEYES,
            "tool_calls": [
                {"tool": "get_test_results", "latency_ms": 200, "status": "success"},
                {"tool": "get_agents", "latency_ms": 150, "status": "success"},
            ],
        }
        state = adapter.ingest_session_log(log)
        assert "thousandeyes.get_test_results" in state["agents"]
        assert "thousandeyes.get_agents" in state["agents"]

    def test_returns_system_state(self, adapter):
        log = {"product": CiscoMCPAdapter.WEBEX, "tool_calls": []}
        state = adapter.ingest_session_log(log)
        assert "agents" in state
        assert "anomalies" in state
        assert "recommendations" in state

    def test_null_tool_calls_does_not_raise(self, adapter):
        log = {"product": CiscoMCPAdapter.CATALYST_CENTER, "tool_calls": None}
        state = adapter.ingest_session_log(log)
        assert state["agents"] == {}

    def test_error_call_in_log(self, adapter, monitor):
        log = {
            "product": CiscoMCPAdapter.NEXUS_DASHBOARD,
            "tool_calls": [
                {
                    "tool": "get_endpoints",
                    "latency_ms": 1200,
                    "status": "error",
                    "error": {"code": "503", "message": "Service Unavailable"},
                }
            ],
        }
        state = adapter.ingest_session_log(log)
        agent = state["agents"]["nexus_dashboard.get_endpoints"]
        assert agent["error_rate"] > 0

    def test_stream_output_type_uses_generation_latency(self, adapter, monitor):
        log = {
            "product": CiscoMCPAdapter.WEBEX,
            "tool_calls": [
                {
                    "tool": "stream_messages",
                    "latency_ms": 45000,
                    "status": "success",
                    "output_type": "stream",
                }
            ],
        }
        adapter.ingest_session_log(log)
        state = monitor.get_system_state()
        agent = state["agents"]["webex.stream_messages"]
        assert agent["generation_observations"] == 1
        assert agent["machine_observations"] == 0

    def test_missing_product_defaults_to_unknown(self, adapter, monitor):
        log = {
            "tool_calls": [
                {"tool": "some_tool", "latency_ms": 100, "status": "success"}
            ]
        }
        adapter.ingest_session_log(log)
        state = monitor.get_system_state()
        assert "unknown.some_tool" in state["agents"]

    def test_high_error_rate_triggers_anomaly(self, adapter, monitor):
        log = {
            "product": CiscoMCPAdapter.CATALYST_SDWAN,
            "tool_calls": [
                {"tool": "get_device_status", "latency_ms": 100, "status": "error"}
                for _ in range(15)
            ],
        }
        state = adapter.ingest_session_log(log)
        names = {a["name"] for a in state["anomalies"]}
        assert "error_rate_high" in names


class TestIngestMcpTrace:
    def test_success_response_creates_observation(self, adapter, monitor):
        trace = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_test_results", "arguments": {"test_id": "1"}}},
            {"jsonrpc": "2.0", "id": 1, "_latency_ms": 200,
             "result": {"content": [], "isError": False}},
        ]
        state = adapter.ingest_mcp_trace(trace, product=CiscoMCPAdapter.THOUSANDEYES)
        assert "thousandeyes.get_test_results" in state["agents"]

    def test_jsonrpc_error_counts_as_failed(self, adapter, monitor):
        trace = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "missing_tool", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 1, "_latency_ms": 10,
             "error": {"code": -32601, "message": "Method not found"}},
        ]
        state = adapter.ingest_mcp_trace(trace, product=CiscoMCPAdapter.THOUSANDEYES)
        agent = state["agents"]["thousandeyes.missing_tool"]
        assert agent["error_rate"] > 0

    def test_is_error_true_counts_as_failed(self, adapter, monitor):
        trace = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_alerts", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 1, "_latency_ms": 80,
             "result": {"content": [], "isError": True}},
        ]
        state = adapter.ingest_mcp_trace(trace, product=CiscoMCPAdapter.THOUSANDEYES)
        assert state["agents"]["thousandeyes.get_alerts"]["error_rate"] > 0

    def test_uses_adapter_product_when_not_passed(self, monitor):
        adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.WEBEX)
        trace = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "send_message", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 1, "_latency_ms": 100,
             "result": {"content": [], "isError": False}},
        ]
        state = adapter.ingest_mcp_trace(trace)
        assert "webex.send_message" in state["agents"]

    def test_unmatched_response_ignored(self, adapter, monitor):
        trace = [
            # Response with no matching request
            {"jsonrpc": "2.0", "id": 99, "_latency_ms": 50,
             "result": {"content": [], "isError": False}},
        ]
        state = adapter.ingest_mcp_trace(trace, product=CiscoMCPAdapter.THOUSANDEYES)
        assert state["agents"] == {}
