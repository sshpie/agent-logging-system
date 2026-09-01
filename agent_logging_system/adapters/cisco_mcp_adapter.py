"""Cisco MCP adapter: monitor a fleet of Cisco MCP server tool calls.

Cisco MCP servers (ThousandEyes, Webex, Catalyst Center, Nexus Dashboard,
Catalyst SD-WAN, IOS-XE) expose tools to agentic pipelines via the Model
Context Protocol. Each tool call becomes an Observation attributed to a
compound agent_id: "<product>.<tool_name>". This gives per-tool error rates
and per-product latency trends visible in a single monitor.

Usage (direct):

    from agent_logging_system import LoggingAgent
    from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter

    monitor = LoggingAgent()
    adapter = CiscoMCPAdapter(monitor)

    adapter.log_tool_call(
        tool_name="get_alerts",
        latency_ms=340,
        status="success",
        product=CiscoMCPAdapter.THOUSANDEYES,
        input_data={"agent_id": "1234"},
        output_data={"alerts": [...]},
    )

    print(adapter.get_state()["anomalies"])

Usage (batch from session log):

    adapter.ingest_session_log({
        "tool_calls": [
            {"tool": "get_tests", "product": "thousandeyes",
             "latency_ms": 280, "status": "success",
             "input": {...}, "output": {...}},
            ...
        ]
    })
"""
from typing import Any, Dict, List, Optional

from .base_adapter import BaseAdapter
from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.observation import LATENCY_MACHINE


class CiscoMCPAdapter(BaseAdapter):
    """Observe Cisco MCP server tool calls as per-tool, per-product agents."""

    THOUSANDEYES = "thousandeyes"
    WEBEX = "webex"
    CATALYST_CENTER = "catalyst_center"
    NEXUS_DASHBOARD = "nexus_dashboard"
    CATALYST_SDWAN = "catalyst_sdwan"
    IOS_XE = "ios_xe"

    KNOWN_PRODUCTS = {
        THOUSANDEYES,
        WEBEX,
        CATALYST_CENTER,
        NEXUS_DASHBOARD,
        CATALYST_SDWAN,
        IOS_XE,
    }

    def wrap_agent(self, agent: Any) -> Any:
        return agent

    def log_tool_call(
        self,
        tool_name: str,
        latency_ms: float,
        status: str,
        product: str = THOUSANDEYES,
        input_data: Any = None,
        output_data: Any = None,
        error_details: Optional[Dict[str, str]] = None,
    ) -> None:
        """Emit one Observation for a single Cisco MCP tool call.

        agent_id is "<product>.<tool_name>" so per-tool error rates are
        tracked independently while the product prefix groups them logically.
        """
        product = product.lower()
        agent_id = f"{product}.{tool_name}"
        confidence = 1.0 if status == "success" else 0.5

        self.emit_observation(
            agent_id=agent_id,
            action="tool_call",
            input_data=input_data or {},
            output_data=output_data or {},
            latency_ms=latency_ms,
            status=status,
            confidence=confidence,
            error_details=error_details,
            latency_kind=LATENCY_MACHINE,
        )

    def ingest_session_log(self, session_log: Dict[str, Any]) -> Dict[str, Any]:
        """Ingest a batch of tool calls from an MCP session log dict.

        Expected shape:

            {
                "tool_calls": [
                    {
                        "tool": "<tool_name>",
                        "product": "<product>",       # optional, defaults to "thousandeyes"
                        "latency_ms": <float>,
                        "status": "<success|failed|timeout|retry>",
                        "input": <any>,               # optional
                        "output": <any>,              # optional
                        "error": "<message>",         # optional
                    },
                    ...
                ]
            }

        Returns the full system_state snapshot after ingestion.
        """
        calls: List[Dict[str, Any]] = session_log.get("tool_calls") or []
        for call in calls:
            tool_name = call.get("tool") or "unknown"
            product = (call.get("product") or self.THOUSANDEYES).lower()
            latency_ms = float(call.get("latency_ms") or 0.0)
            status = call.get("status") or "success"
            input_data = call.get("input")
            output_data = call.get("output")
            error_msg = call.get("error")
            error_details = {"message": error_msg} if error_msg else None

            self.log_tool_call(
                tool_name=tool_name,
                latency_ms=latency_ms,
                status=status,
                product=product,
                input_data=input_data,
                output_data=output_data,
                error_details=error_details,
            )

        return self.get_state()
