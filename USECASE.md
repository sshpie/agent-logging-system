# Use Case: Operational Observability for Cisco AI Agent Pipelines

## Problem

Cisco platforms — Meraki, ThousandEyes, Webex, Catalyst Center, Nexus Dashboard, NSO, SD-WAN, IOS XE — integrate into AI agent pipelines through MCP servers and REST APIs. When a tool inside those systems degrades — slow responses, rising error rates, 429 throttling, unreachable devices — the pipeline continues silently. The calling agent produces stale or incomplete results. No alert fires. The operator finds out from downstream impact, not from the tool.

Standard logging captures that calls happened. It does not detect that a specific tool is degrading relative to its own baseline, or that Meraki rate limit budget is nearly exhausted, or that an NSO `sync-from` is failing consistently on one device.

## Solution

`agent-logging-system` places a monitor at the adapter layer between your AI agents and Cisco systems. Every tool call, API call, or RESTCONF operation becomes a structured `Observation`. The monitor tracks a rolling window per tool, computes baseline-relative statistics, and fires named recommendations when a component crosses a threshold.

Each tool gets its own rolling window — `thousandeyes.get_alerts` and `webex.send_message` are tracked independently. A flaky tool appears as a named anomaly. The monitor can send Webex Adaptive Card alerts and expose a queryable MCP endpoint so any Cisco AI agent can check fleet health directly.

## Architecture

```
Cisco AI agent pipeline
         │
         ├─ Meraki Dashboard API ───────► MerakiAdapter
         │                                  rate limit tracking
         │                                  429 anomaly (HIGH)
         │
         ├─ NSO RESTCONF / PyAPI ────────► NSOAdapter
         │                                  per-operation latency classes
         │                                  device-scoped failure tracking
         │
         ├─ Webex Messaging MCP ─────────► WebexMessagingMCPAdapter
         │                                  24 tools pre-classified
         │                                  wrap_agent() auto-intercepts
         │
         ├─ ThousandEyes / Catalyst ─────► CiscoMCPAdapter
         │  Center / Nexus / SD-WAN /        <product>.<tool_name> tracking
         │  IOS XE / Webex                   MCP JSON-RPC trace ingest
         │
         └─ Any MCP-compatible server ──► CiscoMCPAdapter
                                            ingest_mcp_trace()
                                            │
                                            ▼
                               LoggingAgent.ingest(Observation)
                                            │
                                            ▼
                               StateModel — rolling 20-obs window per tool
                               AnomalyDetector — latency_high / error_rate_high
                                            │
                                            ├─► WebexNotifier → Webex room (Adaptive Card)
                                            │
                                            └─► WebexBotHandler → NOC operator interface
                                                als status / anomalies / check / recommend
                                                polling mode (no HTTPS) or webhook mode
```

## Cisco products covered

| Adapter | Product | Key behavior |
|---------|---------|-------------|
| `MerakiAdapter` | Meraki Dashboard API | Rate limit header tracking; MEDIUM at 20% remaining; HIGH on 429 |
| `NSOAdapter` | Crosswork NSO | `sync-from`/`sync-all` = generation latency (no alarm); `commit`/`check-sync` = machine latency |
| `WebexMessagingMCPAdapter` | Webex Messaging MCP Server | 24 tools pre-classified (read/write/stream); streaming tools never alarm on latency |
| `CiscoMCPAdapter` | ThousandEyes, Webex, Catalyst Center, Nexus Dashboard, Catalyst SD-WAN, IOS XE | `<product>.<tool_name>` per-tool windows; MCP JSON-RPC 2.0 trace ingest |

## Key behaviors

- **Baseline-relative alarms** — a consistently slow tool never trips. A tool that spikes 3x its own baseline does.
- **Generation latency never alarms** — streaming tools, paginated API calls, NSO bulk syncs log as `LATENCY_GENERATION`. Any magnitude is expected.
- **Meraki rate limit tracking** — `X-RateLimit-Remaining` is parsed on every call. A budget below 20% fires `MEDIUM`; a 429 fires `HIGH` immediately.
- **NSO operation-class awareness** — `sync-from` and `re-deploy-all` are classified as long-running. `commit` and `check-sync` are machine-latency operations.
- **Webex bot command interface** — `WebexBotHandler` lets NOC operators query live monitor state from any Webex space (`als status`, `als anomalies HIGH`, `als check <agent_id>`). Responds with Adaptive Cards. Polling mode requires no inbound HTTPS; webhook mode integrates with Webex event push.
- **Webex Adaptive Cards** — `WebexNotifier.notify_if_anomalies_card()` sends structured cards with severity color-coding and per-anomaly recommendation rows.
- **No external dependencies** — stdlib only. No collector, no database, no network calls required.

## Example: Meraki rate limit anomaly

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters import MerakiAdapter
from agent_logging_system.alerting import WebexNotifier

monitor = LoggingAgent()
adapter = MerakiAdapter(monitor, organization_id="your-org-id")

with adapter.observe("networks.getNetworkDevices") as obs:
    result = dashboard.networks.getNetworkDevices(networkId=net_id)
    obs.rate_limit_remaining = int(resp.headers["X-RateLimit-Remaining"])

state = monitor.get_system_state()

notifier = WebexNotifier(bot_token="...", room_id="...", min_level="MEDIUM")
notifier.notify_if_anomalies_card(state)
```

## Example: Webex Messaging MCP auto-intercept

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters import WebexMessagingMCPAdapter

monitor = LoggingAgent()
adapter = WebexMessagingMCPAdapter(monitor)
session = adapter.wrap_agent(mcp_client_session)

# Every tool call is now automatically observed.
await session.async_call_tool("webex-create-message", {"roomId": "...", "text": "Hello"})
```

## Example: Webex bot — NOC operator command interface

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.alerting import WebexNotifier, WebexBotHandler
from agent_logging_system.alerting.webex_bot import WebexBotPoller

monitor  = LoggingAgent()
notifier = WebexNotifier(bot_token="...", room_id="...", min_level="LOW")
handler  = WebexBotHandler(monitor, notifier)

# Polling mode — no inbound HTTPS required.
poller = WebexBotPoller(handler, bot_token="...", room_ids=["room-id"], poll_interval=5.0)
poller.poll_forever()
```

Operators send commands from any Webex client. The bot replies with an Adaptive Card:

| Command | Response |
|---------|---------|
| `als status` | Fleet snapshot: agent count + anomaly counts by level |
| `als anomalies HIGH` | Anomalies at HIGH severity with recommendations |
| `als check nso.sync_from.edge-router-01` | Rolling-window state for one agent |
| `als recommend` | Full recommendation list |

## Target users

Network operations and AI platform teams running agent pipelines against Cisco MCP servers and REST APIs. The monitor surfaces the tool-level signal that aggregate logs miss — before downstream impact, not after.
