"""Cisco ThousandEyes MCP monitoring example.

Shows three integration paths:
  1. wrap_agent()       — automatic interception of an mcp.ClientSession
  2. log_tool_call()    — manual logging when you control the call site
  3. ingest_mcp_trace() — post-hoc replay of a real MCP protocol message trace

Run with any Python 3.9+ environment — no mcp package required for paths 2 and 3.
"""
import asyncio

from agent_logging_system import LoggingAgent
from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter
from agent_logging_system.alerting import WebexNotifier


# ── PATH 1: automatic interception via wrap_agent() ──────────────────────────

async def monitor_with_wrapped_session(mcp_client_session):
    """Pass an mcp.ClientSession through wrap_agent() — every call_tool() is observed."""
    monitor = LoggingAgent()
    adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.THOUSANDEYES)
    session = adapter.wrap_agent(mcp_client_session)

    # Use wrapper.async_call_tool() for the real async mcp.ClientSession.
    await session.async_call_tool("get_test_results", {"test_id": "123"})
    await session.async_call_tool("get_agents", {"account_id": "456"})
    await session.async_call_tool("get_alerts", {"active": True})

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── PATH 2: manual logging ────────────────────────────────────────────────────

def monitor_with_manual_logging():
    """Log tool calls manually — use when you control the call site directly."""
    monitor = LoggingAgent()
    adapter = CiscoMCPAdapter(monitor)

    calls = [
        ("get_test_results", CiscoMCPAdapter.THOUSANDEYES, 280, "success"),
        ("get_agents",       CiscoMCPAdapter.THOUSANDEYES, 190, "success"),
        ("get_alerts",       CiscoMCPAdapter.THOUSANDEYES, 8400, "success"),  # spike
        ("send_message",     CiscoMCPAdapter.WEBEX,        95,  "success"),
        ("get_devices",      CiscoMCPAdapter.CATALYST_CENTER, 500, "error"),
    ]

    for tool_name, product, latency_ms, status in calls:
        adapter.log_tool_call(
            tool_name=tool_name,
            product=product,
            latency_ms=latency_ms,
            status=status,
        )

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── PATH 3: ingest_mcp_trace() — real MCP JSON-RPC wire format ───────────────

def monitor_with_mcp_trace():
    """Replay a recorded MCP protocol message trace. _latency_ms is optional metadata."""
    monitor = LoggingAgent()
    adapter = CiscoMCPAdapter(monitor, product=CiscoMCPAdapter.THOUSANDEYES)

    trace = [
        # Request
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_test_results", "arguments": {"test_id": "123"}}},
        # Response — _latency_ms is attached by the client; not part of the spec but common
        {"jsonrpc": "2.0", "id": 1, "_latency_ms": 280,
         "result": {"content": [{"type": "text", "text": "..."}], "isError": False}},

        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_alerts", "arguments": {"active": True}}},
        {"jsonrpc": "2.0", "id": 2, "_latency_ms": 7900,
         "result": {"content": [{"type": "text", "text": "..."}], "isError": False}},

        # JSON-RPC level error (tool not found)
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "nonexistent_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "_latency_ms": 12,
         "error": {"code": -32601, "message": "Method not found"}},
    ]

    state = adapter.ingest_mcp_trace(trace)
    _print_state(state)
    return state


# ── Webex alerting ────────────────────────────────────────────────────────────

def alert_to_webex(state, bot_token: str, room_id: str):
    """Forward anomalies from any state dict to a Webex room."""
    notifier = WebexNotifier(
        bot_token=bot_token,
        room_id=room_id,
        min_level="HIGH",
    )
    result = notifier.notify_if_anomalies(state)
    if result:
        print(f"Alert posted to Webex: {result.get('id')}")
    else:
        print("No HIGH anomalies — no alert sent")


def _print_state(state):
    print(f"\nAgents tracked: {len(state['agents'])}")
    for agent_id, data in state["agents"].items():
        print(f"  {agent_id}: avg={data['avg_latency']:.0f}ms errors={data['error_rate']:.0%}")
    if state["anomalies"]:
        print("Anomalies:")
        for a in state["anomalies"]:
            print(f"  [{a['alert_level']}] {a['agent_id']}: {a['name']} -> {a['recommendation']}")
    else:
        print("No anomalies")


if __name__ == "__main__":
    print("=== PATH 2: manual logging ===")
    monitor_with_manual_logging()

    print("\n=== PATH 3: MCP trace ===")
    monitor_with_mcp_trace()
