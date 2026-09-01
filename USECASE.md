# Use Case: Operational Observability for Cisco MCP Server Fleets

## Business Problem

Cisco MCP servers (ThousandEyes, Webex, Catalyst Center, Nexus Dashboard, Catalyst SD-WAN, IOS-XE) expose tools to agentic pipelines via the Model Context Protocol. When an agentic pipeline degrades — slow tool responses, rising error rates, empty outputs — the failure is invisible. There is no native mechanism to watch which MCP tool is breaking, how often, or whether the degradation is worsening.

Network operations teams cannot act on what they cannot see.

## Solution

`agent-logging-system` monitors Cisco MCP tool calls in real time. Each tool call is logged as an Observation attributed to `<product>.<tool_name>`, giving per-tool error rates and per-product latency trends in a single monitor.

The monitor compares each tool against its own baseline (not a fixed threshold), so a steady-slow tool never alarms but a tool that suddenly degrades does. When a threshold is crossed, the monitor names the broken agent and the recommended action.

No database. No external collector. No dependencies. The monitor runs in the same process as the agentic pipeline.

## Target Users

- Network automation engineers running Cisco MCP server fleets
- Platform teams integrating ThousandEyes, Webex, or Catalyst Center into agentic pipelines
- Anyone building multi-agent workflows on top of Cisco infrastructure who needs per-tool observability

## Cisco Products Covered

| Product | MCP Server | Adapter constant |
|---------|-----------|-----------------|
| ThousandEyes | thousandeyes-mcp | `CiscoMCPAdapter.THOUSANDEYES` |
| Webex | webex-mcp | `CiscoMCPAdapter.WEBEX` |
| Catalyst Center | catalyst-center-mcp | `CiscoMCPAdapter.CATALYST_CENTER` |
| Nexus Dashboard | nexus-dashboard-mcp | `CiscoMCPAdapter.NEXUS_DASHBOARD` |
| Catalyst SD-WAN | catalyst-sdwan-mcp | `CiscoMCPAdapter.CATALYST_SDWAN` |
| IOS-XE | ios-xe-mcp | `CiscoMCPAdapter.IOS_XE` |

## Example

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter

monitor = LoggingAgent()
adapter = CiscoMCPAdapter(monitor)

# Log a ThousandEyes tool call
adapter.log_tool_call(
    tool_name="get_alerts",
    latency_ms=340,
    status="success",
    product=CiscoMCPAdapter.THOUSANDEYES,
    input_data={"agent_id": "1234"},
    output_data={"alerts": []},
)

state = adapter.get_state()
print(state["anomalies"])       # [] — no alarm yet
print(state["recommendations"]) # []
```

After repeated slow calls, the monitor trips `latency_high` and recommends `throttle_input` — identifying the exact tool and the exact product without manual inspection.

## Why This Matters

Agentic pipelines fail silently. A ThousandEyes alert tool returning empty results 30% of the time looks like normal operation from the outside. From the inside, network operations is flying blind. `agent-logging-system` makes the invisible visible — per tool, per product, in real time.
