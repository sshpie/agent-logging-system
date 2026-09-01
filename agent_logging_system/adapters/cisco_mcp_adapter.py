"""Cisco MCP adapter: translate Cisco MCP server tool calls into monitor observations.

Tracks per-tool performance across Cisco MCP servers so that a flaky tool
(e.g. thousandeyes.get_alerts returning empty, webex.send_message timing out)
appears as a named anomaly with a recommendation rather than silent degradation.

agent_id = "<product>.<tool_name>" — per-tool-per-product rolling windows.

Supported products:
    CiscoMCPAdapter.THOUSANDEYES     ThousandEyes Enterprise Monitoring
    CiscoMCPAdapter.WEBEX            Webex Collaboration
    CiscoMCPAdapter.CATALYST_CENTER  Catalyst Center (DNA Center)
    CiscoMCPAdapter.NEXUS_DASHBOARD  Nexus Dashboard
    CiscoMCPAdapter.CATALYST_SDWAN   Catalyst SD-WAN
    CiscoMCPAdapter.IOS_XE           IOS XE

Usage:

    from agent_logging_system import LoggingAgent
    from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter

    monitor = LoggingAgent()
    adapter = CiscoMCPAdapter(monitor)

    # Log a single MCP tool call:
    adapter.log_tool_call(
        tool_name="get_test_results",
        product=CiscoMCPAdapter.THOUSANDEYES,
        latency_ms=320,
        status="success",
        input_data={"test_id": "123"},
        output_data={"results": [...]},
    )

    # Bulk import from a recorded MCP session log:
    adapter.ingest_session_log(session_log)

    state = monitor.get_system_state()
    print(state["anomalies"])
"""
from typing import Any, Dict, List, Optional

from .base_adapter import BaseAdapter
from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.observation import LATENCY_MACHINE, LATENCY_GENERATION

# MCP status strings -> Observation status.
_MCP_STATUS_MAP = {
    "success": "success",
    "ok": "success",
    "error": "failed",
    "failed": "failed",
    "timeout": "failed",
    "retry": "retry",
}


def _map_status(raw: str) -> str:
    return _MCP_STATUS_MAP.get((raw or "").lower(), "failed")


class CiscoMCPAdapter(BaseAdapter):
    """Wire Cisco MCP server tool calls into the monitor as structured observations."""

    # Product name constants — use these as the `product` argument.
    THOUSANDEYES = "thousandeyes"
    WEBEX = "webex"
    CATALYST_CENTER = "catalyst_center"
    NEXUS_DASHBOARD = "nexus_dashboard"
    CATALYST_SDWAN = "catalyst_sdwan"
    IOS_XE = "ios_xe"

    def wrap_agent(self, agent: Any) -> Any:
        return agent

    def log_tool_call(
        self,
        tool_name: str,
        product: str,
        latency_ms: float,
        status: str = "success",
        input_data: Any = None,
        output_data: Any = None,
        error_details: Optional[Dict[str, str]] = None,
        latency_kind: str = LATENCY_MACHINE,
    ) -> None:
        """Emit one observation for a single MCP tool invocation.

        Args:
            tool_name:    Name of the MCP tool (e.g. "get_test_results").
            product:      One of the product constants on this class.
            latency_ms:   Round-trip time for the tool call.
            status:       "success" | "error" | "timeout" | "retry" | "failed".
            input_data:   Tool input (will be stored as Observation.input).
            output_data:  Tool output (will be stored as Observation.output).
            error_details: {"code": "...", "message": "..."} on failure.
            latency_kind: LATENCY_MACHINE (default) or LATENCY_GENERATION for
                          streaming/long-running tool calls that should not alarm.
        """
        agent_id = f"{product}.{tool_name}"
        mapped_status = _map_status(status)
        confidence = 1.0 if mapped_status == "success" else 0.5

        self.emit_observation(
            agent_id=agent_id,
            action="tool_call",
            input_data=input_data or {},
            output_data=output_data,
            latency_ms=latency_ms,
            status=mapped_status,
            confidence=confidence,
            error_details=error_details,
            latency_kind=latency_kind,
        )

    def ingest_session_log(self, session_log: Dict[str, Any]) -> Dict[str, Any]:
        """Bulk-import a recorded MCP session log.

        Expects the Cisco MCP session log format:
        {
            "product": "<product_name>",
            "tool_calls": [
                {
                    "tool": "<tool_name>",
                    "latency_ms": 250,
                    "status": "success",
                    "input": {...},
                    "output": {...},
                    "error": null | {"code": "...", "message": "..."}
                },
                ...
            ]
        }

        Returns the full system_state snapshot after ingestion.
        """
        product = session_log.get("product", "unknown")
        tool_calls: List[Dict[str, Any]] = session_log.get("tool_calls") or []

        for call in tool_calls:
            tool_name = call.get("tool", "unknown")
            latency_ms = float(call.get("latency_ms") or 0.0)
            status = call.get("status", "success")
            input_data = call.get("input") or {}
            output_data = call.get("output")
            error_raw = call.get("error")

            error_details = None
            if error_raw and isinstance(error_raw, dict):
                error_details = {
                    "code": str(error_raw.get("code", "")),
                    "message": str(error_raw.get("message", "")),
                }

            # Streaming tool calls (output_type="stream") use generation latency.
            latency_kind = (
                LATENCY_GENERATION
                if call.get("output_type") == "stream"
                else LATENCY_MACHINE
            )

            self.log_tool_call(
                tool_name=tool_name,
                product=product,
                latency_ms=latency_ms,
                status=status,
                input_data=input_data,
                output_data=output_data,
                error_details=error_details,
                latency_kind=latency_kind,
            )

        return self.get_state()
