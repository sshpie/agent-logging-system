# Use Case: Operational Observability for Cisco MCP Server Fleets

## Problem

Cisco MCP servers (ThousandEyes, Webex, Catalyst Center, Nexus Dashboard, SD-WAN, IOS XE) integrate into AI agent pipelines. When a tool inside one of those servers degrades — slow responses, rising error rates, empty outputs — the pipeline continues silently. The calling agent produces stale or incomplete results. No alert fires. The operator finds out from downstream impact, not from the tool.

Standard logging captures that calls happened. It does not detect that a specific tool is degrading relative to its own baseline.

## Solution

`agent-logging-system` places a monitor at the MCP adapter layer. Every tool call becomes a structured `Observation`. The monitor tracks a rolling window per tool, computes baseline-relative statistics, and fires named recommendations when a tool crosses a threshold.

The `CiscoMCPAdapter` translates MCP tool invocations into observations with `agent_id = "<product>.<tool_name>"`. Each tool gets its own rolling window — `thousandeyes.get_alerts` and `webex.send_message` are tracked independently. A flaky tool appears as a named anomaly, not as noise in an aggregate metric.

## How it works

```
Cisco MCP server tool call
            │
            ▼
  CiscoMCPAdapter.log_tool_call()
  (product="thousandeyes", tool="get_alerts", latency_ms=8200, status="success")
            │
            ▼
  agent_id = "thousandeyes.get_alerts"
  LoggingAgent.ingest(Observation(...))
            │
            ▼
  StateModel — rolling 20-obs window per tool
  AnomalyDetector — latency_high if avg exceeds 3x baseline
            │
            ▼
  { anomalies: [{ rule: "latency_high", agent: "thousandeyes.get_alerts" }],
    recommendations: [{ action: "throttle_input" }] }
```

## Key behaviors

- **Baseline-relative alarms** — a consistently slow tool never trips. A tool that spikes 3x its own baseline does.
- **Generation latency never alarms** — streaming tools like Webex event streams log duration as `LATENCY_GENERATION`. Any magnitude is expected.
- **Bulk import** — `ingest_session_log()` replays a full recorded MCP session log. Useful for post-mortem analysis.
- **No external dependencies** — stdlib only. No collector, no database, no network calls.

## Example

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters.cisco_mcp_adapter import CiscoMCPAdapter

monitor = LoggingAgent()
adapter = CiscoMCPAdapter(monitor)

# Log each MCP tool call as it executes:
adapter.log_tool_call(
    tool_name="get_test_results",
    product=CiscoMCPAdapter.THOUSANDEYES,
    latency_ms=8200,
    status="success",
    input_data={"test_id": "123"},
    output_data={"results": []},
)

state = monitor.get_system_state()
for anomaly in state["anomalies"]:
    print(f"{anomaly['agent_id']}: {anomaly['rule']} -> {anomaly['recommendation']}")
```

## Cisco products covered

| Product constant | MCP server |
|-----------------|------------|
| `THOUSANDEYES` | ThousandEyes Enterprise Monitoring |
| `WEBEX` | Webex Collaboration |
| `CATALYST_CENTER` | Catalyst Center |
| `NEXUS_DASHBOARD` | Nexus Dashboard |
| `CATALYST_SDWAN` | Catalyst SD-WAN |
| `IOS_XE` | IOS XE |

## Target users

Network operations teams running AI agents that call Cisco MCP servers in production. The monitor surfaces the tool-level signal that aggregate logs miss.
