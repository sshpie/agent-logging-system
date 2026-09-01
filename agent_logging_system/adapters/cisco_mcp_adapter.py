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

Usage — automatic interception via wrap_agent():

    from mcp import ClientSession
    from agent_logging_system import LoggingAgent
    from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter

    monitor = LoggingAgent()
    adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.THOUSANDEYES)
    session = adapter.wrap_agent(mcp_client_session)

    # Every session.call_tool() is now automatically observed.
    result = await session.call_tool("get_test_results", {"test_id": "123"})

Usage — manual logging:

    adapter.log_tool_call(
        tool_name="get_test_results",
        product=CiscoMCPAdapter.THOUSANDEYES,
        latency_ms=320,
        status="success",
    )

Usage — bulk import of a real MCP protocol session trace:

    adapter.ingest_mcp_trace(trace_messages)   # list of MCP JSON-RPC messages

    state = monitor.get_system_state()
    print(state["anomalies"])
"""
import time
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

# MCP tools that return streaming content — their latency is generation, not machine.
_STREAMING_TOOLS = {
    "stream_events",
    "stream_messages",
    "subscribe",
    "watch",
}


def _map_status(raw: str) -> str:
    return _MCP_STATUS_MAP.get((raw or "").lower(), "failed")


class _WrappedMCPSession:
    """Transparent proxy around an MCP ClientSession that observes every tool call.

    Works with both sync and async call_tool() — whichever the real session has.
    Does not import the `mcp` package; duck-types the interception.
    """

    def __init__(self, session: Any, adapter: "CiscoMCPAdapter", product: str):
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_adapter", adapter)
        object.__setattr__(self, "_product", product)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_session"), name)

    def call_tool(self, tool_name: str, arguments: Optional[Dict] = None, **kwargs):
        """Sync interception — times the call and emits an observation."""
        session = object.__getattribute__(self, "_session")
        adapter = object.__getattribute__(self, "_adapter")
        product = object.__getattribute__(self, "_product")
        t0 = time.monotonic()
        try:
            result = session.call_tool(tool_name, arguments, **kwargs)
            latency_ms = (time.monotonic() - t0) * 1000
            is_error = getattr(result, "isError", False)
            adapter.log_tool_call(
                tool_name=tool_name,
                product=product,
                latency_ms=latency_ms,
                status="failed" if is_error else "success",
                input_data=arguments or {},
                output_data=_extract_content(result),
                latency_kind=(
                    LATENCY_GENERATION if tool_name in _STREAMING_TOOLS else LATENCY_MACHINE
                ),
            )
            return result
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            adapter.log_tool_call(
                tool_name=tool_name,
                product=product,
                latency_ms=latency_ms,
                status="failed",
                input_data=arguments or {},
                error_details={"code": type(exc).__name__, "message": str(exc)},
            )
            raise

    async def async_call_tool(
        self, tool_name: str, arguments: Optional[Dict] = None, **kwargs
    ):
        """Async interception for mcp.ClientSession.call_tool (which is a coroutine)."""
        session = object.__getattribute__(self, "_session")
        adapter = object.__getattribute__(self, "_adapter")
        product = object.__getattribute__(self, "_product")
        t0 = time.monotonic()
        try:
            result = await session.call_tool(tool_name, arguments, **kwargs)
            latency_ms = (time.monotonic() - t0) * 1000
            is_error = getattr(result, "isError", False)
            adapter.log_tool_call(
                tool_name=tool_name,
                product=product,
                latency_ms=latency_ms,
                status="failed" if is_error else "success",
                input_data=arguments or {},
                output_data=_extract_content(result),
                latency_kind=(
                    LATENCY_GENERATION if tool_name in _STREAMING_TOOLS else LATENCY_MACHINE
                ),
            )
            return result
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            adapter.log_tool_call(
                tool_name=tool_name,
                product=product,
                latency_ms=latency_ms,
                status="failed",
                input_data=arguments or {},
                error_details={"code": type(exc).__name__, "message": str(exc)},
            )
            raise


def _extract_content(result: Any) -> Any:
    """Pull serializable content out of a CallToolResult without importing mcp."""
    content = getattr(result, "content", None)
    if content is None:
        return None
    if isinstance(content, list):
        return [
            getattr(item, "text", None) or getattr(item, "data", None) or str(item)
            for item in content
        ]
    return str(content)


class CiscoMCPAdapter(BaseAdapter):
    """Wire Cisco MCP server tool calls into the monitor as structured observations."""

    # Product name constants — use these as the `product` argument.
    THOUSANDEYES = "thousandeyes"
    WEBEX = "webex"
    CATALYST_CENTER = "catalyst_center"
    NEXUS_DASHBOARD = "nexus_dashboard"
    CATALYST_SDWAN = "catalyst_sdwan"
    IOS_XE = "ios_xe"

    def __init__(self, logging_agent: LoggingAgent, product: str = ""):
        super().__init__(logging_agent)
        self._product = product

    def wrap_agent(self, agent: Any) -> "_WrappedMCPSession":
        """Wrap an MCP ClientSession so every call_tool() is automatically observed.

        The returned wrapper is a transparent proxy — all attributes and methods
        pass through to the real session. call_tool() is intercepted; everything
        else is untouched.

        For async sessions (mcp.ClientSession), use wrapper.async_call_tool()
        instead of the session's call_tool() directly.
        """
        if not self._product:
            raise ValueError(
                "CiscoMCPAdapter requires a product when wrapping an MCP session. "
                "Pass product=CiscoMCPAdapter.THOUSANDEYES (or another constant) "
                "to the adapter constructor."
            )
        return _WrappedMCPSession(agent, self, self._product)

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

    def ingest_mcp_trace(
        self,
        messages: List[Dict[str, Any]],
        product: str = "",
    ) -> Dict[str, Any]:
        """Parse a real MCP protocol JSON-RPC message trace into observations.

        Accepts the wire-format message list that the MCP Python SDK (or any
        MCP-compliant client) produces — pairs of tools/call requests and
        tools/call responses matched by id.

        Format (list of JSON-RPC 2.0 messages):
            [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "get_test_results", "arguments": {...}}},
                {"jsonrpc": "2.0", "id": 1,
                 "result": {"content": [...], "isError": false}},
                ...
            ]

        If a response carries "error" at the JSON-RPC level (method not found,
        invalid params, etc.) it is also counted as failed.

        product defaults to self._product set at construction time.
        """
        use_product = product or self._product or "unknown"

        # Index requests by id so we can match them to responses.
        requests: Dict[Any, Dict] = {}
        for msg in messages:
            if msg.get("method") == "tools/call":
                requests[msg.get("id")] = msg

        # Walk responses and emit one observation per matched request/response pair.
        for msg in messages:
            msg_id = msg.get("id")
            if msg_id not in requests:
                continue
            # Must be a response (has "result" or "error", no "method").
            if "method" in msg:
                continue

            req = requests[msg_id]
            params = req.get("params") or {}
            tool_name = params.get("name", "unknown")
            arguments = params.get("arguments") or {}

            if "error" in msg:
                err = msg["error"]
                error_details = {
                    "code": str(err.get("code", "")),
                    "message": str(err.get("message", "")),
                }
                self.log_tool_call(
                    tool_name=tool_name,
                    product=use_product,
                    latency_ms=float(msg.get("_latency_ms") or 0.0),
                    status="failed",
                    input_data=arguments,
                    error_details=error_details,
                )
            else:
                result = msg.get("result") or {}
                is_error = result.get("isError", False)
                content = result.get("content", [])
                latency_kind = (
                    LATENCY_GENERATION if tool_name in _STREAMING_TOOLS else LATENCY_MACHINE
                )
                self.log_tool_call(
                    tool_name=tool_name,
                    product=use_product,
                    latency_ms=float(msg.get("_latency_ms") or 0.0),
                    status="failed" if is_error else "success",
                    input_data=arguments,
                    output_data=content,
                    latency_kind=latency_kind,
                )

        return self.get_state()
