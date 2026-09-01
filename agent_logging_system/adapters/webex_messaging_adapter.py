"""Webex Messaging MCP Server adapter.

Pre-configured for the Cisco Webex Messaging MCP Server at:
    https://mcp.webexapis.com/mcp/webex-messaging

Knows all 24 tool names, classifies read vs write operations, and applies
correct latency categorization. Streaming/subscription tools never trigger
latency alarms.

Auth: OAuth 2.0 Bearer token + spark:mcp scope
Clients: Claude Code, Claude Desktop, Cursor, VS Code, Codex, Copilot Studio,
         Gemini CLI, Amazon Q

Tool naming convention: webex-{verb}-{noun}

Usage — wrap an MCP ClientSession pointed at the Webex Messaging server:

    from mcp import ClientSession
    from agent_logging_system import LoggingAgent
    from agent_logging_system.adapters.webex_messaging_adapter import WebexMessagingMCPAdapter

    monitor = LoggingAgent()
    adapter = WebexMessagingMCPAdapter(monitor)
    session = adapter.wrap_agent(mcp_client_session)

    # All tool calls auto-observed.
    await session.async_call_tool("webex-create-message", {"roomId": "...", "text": "Hello"})

Usage — manual log:

    adapter.log_tool_call(
        tool_name="webex-send-message",
        latency_ms=95,
        status="success",
    )

Usage — bulk MCP trace ingest (wire format):

    adapter.ingest_mcp_trace(trace_messages)
"""
from typing import Any, Optional, Dict

from .cisco_mcp_adapter import CiscoMCPAdapter
from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.observation import LATENCY_MACHINE, LATENCY_GENERATION

# Full tool catalog for the Webex Messaging MCP Server.
# Convention: webex-{verb}-{noun}
# Streaming tools never alarm on latency regardless of magnitude.

WEBEX_MESSAGING_TOOLS: dict = {
    # Messages
    "webex-create-message":   {"kind": "write"},
    "webex-get-message":      {"kind": "read"},
    "webex-list-messages":    {"kind": "read"},
    "webex-delete-message":   {"kind": "write"},
    "webex-edit-message":     {"kind": "write"},
    # Rooms / Spaces
    "webex-create-space":     {"kind": "write"},
    "webex-get-space":        {"kind": "read"},
    "webex-list-spaces":      {"kind": "read"},
    "webex-update-space":     {"kind": "write"},
    "webex-delete-space":     {"kind": "write"},
    # Memberships
    "webex-add-member":       {"kind": "write"},
    "webex-get-member":       {"kind": "read"},
    "webex-list-members":     {"kind": "read"},
    "webex-remove-member":    {"kind": "write"},
    # People
    "webex-get-person":       {"kind": "read"},
    "webex-list-people":      {"kind": "read"},
    # Attachments / Files
    "webex-upload-file":      {"kind": "write"},
    "webex-get-file":         {"kind": "read"},
    "webex-send-attachment":  {"kind": "write"},
    # Webhooks
    "webex-create-webhook":   {"kind": "write"},
    "webex-get-webhook":      {"kind": "read"},
    "webex-list-webhooks":    {"kind": "read"},
    "webex-delete-webhook":   {"kind": "write"},
    # Streaming / subscriptions
    "webex-subscribe-events": {"kind": "stream"},
}

_STREAM_TOOLS = {k for k, v in WEBEX_MESSAGING_TOOLS.items() if v["kind"] == "stream"}


class WebexMessagingMCPAdapter(CiscoMCPAdapter):
    """CiscoMCPAdapter pre-configured for the Webex Messaging MCP Server.

    Inherits all CiscoMCPAdapter methods. The product is fixed to "webex_messaging"
    and streaming tools are pre-classified so they never trigger latency anomalies.

    This adapter understands the semantic difference between:
        read  tools  — safe to retry; failure may indicate API degradation
        write tools  — side-effectful; repeated failure = message delivery risk
        stream tools — long-running; latency is irrelevant
    """

    def __init__(self, logging_agent: LoggingAgent):
        super().__init__(logging_agent, product=CiscoMCPAdapter.WEBEX_MESSAGING)

    def log_tool_call(
        self,
        tool_name: str,
        product: str = "",
        latency_ms: float = 0.0,
        status: str = "success",
        input_data: Any = None,
        output_data: Any = None,
        error_details: Optional[Dict[str, str]] = None,
        latency_kind: str = LATENCY_MACHINE,
    ) -> None:
        """Override to apply Webex Messaging tool classification automatically.

        Streaming tools always use LATENCY_GENERATION regardless of what the
        caller passes. All other classification logic is inherited.
        """
        effective_kind = latency_kind
        if tool_name in _STREAM_TOOLS:
            effective_kind = LATENCY_GENERATION

        super().log_tool_call(
            tool_name=tool_name,
            product=CiscoMCPAdapter.WEBEX_MESSAGING,
            latency_ms=latency_ms,
            status=status,
            input_data=input_data,
            output_data=output_data,
            error_details=error_details,
            latency_kind=effective_kind,
        )

    @classmethod
    def tool_names(cls):
        """Return the list of known Webex Messaging MCP tool names."""
        return list(WEBEX_MESSAGING_TOOLS.keys())

    @classmethod
    def tool_kind(cls, tool_name: str) -> str:
        """Return "read", "write", or "stream" for a tool name, or "unknown"."""
        return WEBEX_MESSAGING_TOOLS.get(tool_name, {}).get("kind", "unknown")
