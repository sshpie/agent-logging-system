<h1 align="center">agent-logging-system</h1>

<div align="center">
<img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+">
<img src="https://img.shields.io/badge/dependencies-none-brightgreen?style=flat-square" alt="No dependencies">
<img src="https://img.shields.io/badge/Cisco-DevNet-1BA0D7?style=flat-square&logo=cisco&logoColor=white" alt="Cisco DevNet">
<a href="https://github.com/sshpie/agent-logging-system/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/sshpie/agent-logging-system/tests.yml?label=tests&style=flat-square" alt="tests"></a>
<a href="https://github.com/sshpie/agent-logging-system/blob/main/LICENSE"><img src="https://img.shields.io/github/license/sshpie/agent-logging-system?style=flat-square" alt="MIT"></a>
</div>

<br />

<div align="center">
Operational monitor for <strong>Cisco AI agent pipelines</strong> — Meraki, NSO, Webex, ThousandEyes, Catalyst Center.<br>
Per-tool rolling windows, baseline-relative anomaly detection, Webex Adaptive Card alerts, and a NOC bot interface.<br>
</div>

---

## Table of Contents

1. [Overview](#overview)
2. [Cisco Products Covered](#cisco-products-covered)
3. [Features](#features)
4. [Architecture](#architecture)
5. [Requirements](#requirements)
6. [Installation](#installation)
7. [Quick Start](#quick-start)
8. [Components](#components)
9. [Observation Schema](#observation-schema)
10. [Latency Kinds](#latency-kinds)
11. [Default Rules](#default-rules)
12. [Adapters](#adapters)
    - [Cisco Adapters](#cisco-adapters)
    - [General Adapters](#general-adapters)
13. [Webex Alerting](#webex-alerting)
14. [Webex Bot — NOC Command Interface](#webex-bot--noc-command-interface)
15. [Custom Rules](#custom-rules)
16. [Examples](#examples)
17. [Use Case](#use-case)
18. [Scope](#scope)
19. [Getting Help](#getting-help)
20. [Contributing](#contributing)

---

## Overview

When Cisco platforms — Meraki, ThousandEyes, Webex, Catalyst Center, NSO — power an AI agent pipeline, degradation is invisible at the tool call level. A Meraki endpoint throttles silently. An NSO `sync-from` starts failing on one device. A ThousandEyes tool slows down. The calling agent produces stale or incomplete results. No alert fires.

`agent-logging-system` sits between your AI agents and Cisco systems. Every tool call, API call, or RESTCONF operation becomes a structured `Observation`. The monitor maintains a rolling window per tool, computes baseline-relative statistics, and fires named anomalies with concrete recommendations when a component degrades.

The design applies OT/ICS shift-operations discipline to AI pipelines: track the trend, not the snapshot, and catch a degrading tool before it corrupts downstream output.

---

## Cisco Products Covered

| Adapter | Cisco Product | Key Capability |
|---------|--------------|----------------|
| `CiscoMCPAdapter` | ThousandEyes, Catalyst Center, Nexus Dashboard, Catalyst SD-WAN, IOS XE, Webex | Per-tool rolling windows via MCP JSON-RPC 2.0 |
| `WebexMessagingMCPAdapter` | Webex Messaging MCP Server | 24 tools pre-classified; `wrap_agent()` auto-intercepts all calls |
| `MerakiAdapter` | Meraki Dashboard API | `X-RateLimit-Remaining` tracked per call; MEDIUM at 20% budget, HIGH on 429 |
| `NSOAdapter` | Crosswork NSO | Operation-class latency; `sync-from`/`sync-all` never alarm; log file ingestion replaces grep triage |

---

## Features

- **Per-tool `Observation` schema** — timestamp, agent_id, action, input, output, latency_ms, status, confidence, latency_kind
- **Rolling 20-observation window** per agent, tracked by `StateModel`
- **Baseline-relative alarms** — each tool compared against its own baseline, not a global threshold
- **Two latency kinds** — `machine` (alarmable) and `generation` (never alarms); streaming outputs and bulk syncs never trip the latency rule
- **Three built-in rules** — `latency_high`, `error_rate_high`, `queue_buildup`
- **Named recommendations** — every anomaly maps to a concrete action
- **Custom rules** via `AnomalyRule(name, check, alert_level, recommendation)`
- **O(1) ingest**, incremental scan (~15–27 µs for 5 agents)
- **Meraki rate limit tracking** — `X-RateLimit-Remaining` parsed on every call; pre-429 warning at 20%
- **NSO log ingestion** — parse `ncs.log`, `devel.log`, `audit.log`, `ncserr.log.*` into observations; live `follow_log_file()` mode replaces `tail -F | grep`
- **Webex Adaptive Card alerts** — severity color-coding, anomaly table, recommendation rows
- **Webex bot command interface** — `als status/anomalies/check/recommend` from any Webex space; polling mode requires no inbound HTTPS
- **Zero external dependencies** — stdlib only; no database, no collector, no network calls required

---

## Architecture

```
Cisco AI agent pipeline
        │
        ├─ MCP tool calls ──────────────► CiscoMCPAdapter / WebexMessagingMCPAdapter
        │                                   <product>.<tool_name> rolling windows
        │                                   wrap_agent() auto-intercepts sessions
        │
        ├─ Meraki Dashboard API ────────► MerakiAdapter
        │                                   X-RateLimit-Remaining per call
        │                                   MEDIUM at 20% budget / HIGH on 429
        │
        ├─ NSO RESTCONF / PyAPI / logs ─► NSOAdapter
        │                                   operation-class latency classification
        │                                   ingest_log_file() / follow_log_file()
        │
        └─ Any framework ───────────────► BaseAdapter (subclass)
                                            emit_observation()
                                                    │
                                                    ▼
                                     LoggingAgent.ingest()          ← O(1)
                                                    │
                                                    ▼
                                              StateModel
                                        rolling window — 20 obs/agent
                                        machine vs generation latency
                                                    │
                                              AnomalyDetector
                                        latency_high / error_rate_high
                                        queue_buildup / custom rules
                                                    │
                                         RecommendationEngine
                                                    │
                             ┌──────────────────────┴──────────────────────┐
                             ▼                                             ▼
                     { agents, anomalies,                        WebexNotifier
                       recommendations }                    Adaptive Card → Webex room
                                                                           │
                                                                  WebexBotHandler
                                                            NOC operator query interface
                                                            als status / anomalies / check
```

---

## Requirements

- Python 3.9 or later
- No external dependencies — stdlib only

**Optional dev dependencies** (`pip install -e ".[dev]"`):

| Package | Purpose |
|---------|---------|
| `pytest>=7.0` | Test suite |

---

## Installation

```bash
git clone https://github.com/sshpie/agent-logging-system
cd agent-logging-system
pip install -e .
```

---

## Quick Start

Monitor a Meraki Dashboard API call in three lines:

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters import MerakiAdapter
from agent_logging_system.alerting import WebexNotifier

monitor  = LoggingAgent()
adapter  = MerakiAdapter(monitor, organization_id="your-org-id")
notifier = WebexNotifier(bot_token="...", room_id="...", min_level="MEDIUM")

with adapter.observe("networks.getNetworkDevices") as obs:
    result = dashboard.networks.getNetworkDevices(networkId=net_id)
    obs.rate_limit_remaining = int(resp.headers["X-RateLimit-Remaining"])

state = monitor.get_system_state()
notifier.notify_if_anomalies_card(state)
```

If the Meraki budget drops below 20% or a 429 fires, a Webex Adaptive Card alert goes out immediately — before the next call.

---

## Components

| Component | Responsibility |
|-----------|----------------|
| `Observation` | Structured unit: timestamp, agent_id, action, input, output, latency_ms, status, confidence, latency_kind |
| `StateModel` | Rolling window (20 obs/agent), error rate, machine vs generation latency counts |
| `AnomalyDetector` | Evaluates threshold rules against per-agent state snapshots |
| `RecommendationEngine` | Maps anomaly name to concrete remediation action |
| `LoggingAgent` | `ingest()` + `get_system_state()`; O(1) ingest, incremental scan |
| `adapters/` | Cisco-specific and general adapters; extend `BaseAdapter` for any system |
| `alerting/` | `WebexNotifier` (Adaptive Cards) and `WebexBotHandler` (NOC command interface) |

---

## Observation Schema

```python
Observation(
    timestamp: str,          # ISO 8601, e.g. "2026-06-02T14:32:00Z"
    agent_id: str,           # unique tool or agent identifier
    action: str,             # api_call, computation, decision, error, ...
    input: Any,              # input to the action
    output: Any = None,      # output of the action
    latency_ms: float = 0.0, # duration in ms
    status: str = "success", # success | retry | failed | timeout
    confidence: float = 1.0, # 0.0 to 1.0
    error_details: Optional[Dict[str, str]] = None,
    latency_kind: str = "machine",  # "machine" | "generation"
)
```

Only `"failed"` status increments the error count. `"retry"` and `"timeout"` are tracked but do not trip `error_rate_high`.

---

## Latency Kinds

`latency_ms` carries two different quantities, distinguished by `latency_kind`:

| Kind | What it measures | High value means | Trips `latency_high`? |
|------|-----------------|-----------------|----------------------|
| `"machine"` (default) | Execution time of a discrete call | Contention, misconfiguration | Yes |
| `"generation"` | Wall-clock of a long-running output or bulk sync | Expected | Never |

NSO `sync-from`, `sync-all`, and `re-deploy-all` are automatically tagged `generation`. Paginated Meraki calls are tagged `generation`. These can take minutes without triggering an alarm.

```python
from agent_logging_system import Observation, LATENCY_GENERATION

Observation(
    timestamp="...", agent_id="nso.sync_from.core-router", action="sync_from",
    input={"device": "core-router"}, latency_ms=45000,
    latency_kind=LATENCY_GENERATION,   # 45s bulk sync — expected, never alarms
)
```

---

## Default Rules

| Rule | Level | Trips when | Recommendation |
|------|-------|-----------|---------------|
| `latency_high` | HIGH | Machine-kind recent average exceeds the agent's own baseline by 3x, above a 100 ms floor, after a 4-sample warmup | `throttle_input` |
| `error_rate_high` | MEDIUM | Error rate over 10% | `investigate_failures` |
| `queue_buildup` | LOW | Over 10 observations with error rate under 5% | Signal only |

`latency_high` is baseline-relative. A steadily slow tool never trips; a tool that spikes 3x its own normal response time does.

---

## Adapters

### Cisco Adapters

**`CiscoMCPAdapter`** — monitor any Cisco MCP server tool calls. Each tool is tracked as `<product>.<tool_name>`. Three integration paths:

```python
from agent_logging_system.adapters import CiscoMCPAdapter

adapter = CiscoMCPAdapter(LoggingAgent())

# 1. Single call log:
adapter.log_tool_call("get_alerts", product=CiscoMCPAdapter.THOUSANDEYES, latency_ms=340)

# 2. Bulk session log:
adapter.ingest_session_log({"product": "thousandeyes", "tool_calls": [...]})

# 3. Real MCP JSON-RPC 2.0 wire trace:
adapter.ingest_mcp_trace(trace_messages, product=CiscoMCPAdapter.THOUSANDEYES)
```

Products: `THOUSANDEYES`, `WEBEX`, `WEBEX_MESSAGING`, `CATALYST_CENTER`, `NEXUS_DASHBOARD`, `CATALYST_SDWAN`, `IOS_XE`, `MERAKI`, `NSO`.

---

**`WebexMessagingMCPAdapter`** — drop-in wrapper for the [Webex Messaging MCP Server](https://developer.webex.com/mcp/docs/messaging-mcp-server). All 24 tools pre-classified as `read`, `write`, or `stream`. Wrap the session once; every call is automatically observed.

```python
from agent_logging_system.adapters import WebexMessagingMCPAdapter

adapter = WebexMessagingMCPAdapter(LoggingAgent())
session = adapter.wrap_agent(mcp_client_session)

# Every tool call now emits an Observation automatically.
await session.async_call_tool("webex-create-message", {"roomId": "...", "text": "Hello"})
```

---

**`MerakiAdapter`** — monitor Cisco Meraki Dashboard API calls. Tracks `X-RateLimit-Remaining` on every response. Fires a MEDIUM anomaly when the budget drops below 20%; fires HIGH immediately on a 429 — before the next outbound call.

```python
from agent_logging_system.adapters import MerakiAdapter

adapter = MerakiAdapter(LoggingAgent(), organization_id="org-L_123456")

# Context manager — auto-times the call, captures exceptions.
with adapter.observe("networks.getNetworkDevices") as obs:
    result = dashboard.networks.getNetworkDevices(networkId=net_id)
    obs.rate_limit_remaining = int(resp.headers["X-RateLimit-Remaining"])
    obs.result_count = len(result)

# Manual — log after any HTTP client.
adapter.log_api_call("devices.getDevice", status_code=200, latency_ms=110,
                     rate_limit_remaining=2, rate_limit_limit=10)

# Header ingest — pass raw response headers directly.
adapter.ingest_response_headers("networks.getNetworkDevices", 200, 134.0, resp.headers)
```

---

**`NSOAdapter`** — monitor Cisco NSO RESTCONF/NETCONF/PyAPI operations and log files. Operation-class aware: `sync-from`, `sync-all`, and `re-deploy-all` never alarm on latency. `commit` and `check-sync` are machine-latency operations and will alarm if they degrade.

```python
from agent_logging_system.adapters import NSOAdapter

adapter = NSOAdapter(LoggingAgent(), nso_host="nso.corp.example.com")

# 1. Context manager:
with adapter.observe("sync_from", device="edge-router-01"):
    result = maapi_session.sync_from("edge-router-01")

# 2. Manual log:
adapter.log_operation("check_sync", device="edge-router-01", status="success", latency_ms=95)
adapter.log_commit(dry_run=True, latency_ms=155.0, changeset_size=12)

# 3. RESTCONF response:
adapter.ingest_restconf_response("/restconf/operations/tailf-ncs:sync-from", "POST", 200, 5200.0)

# 4. Log file ingest — replaces grep triage scripts:
for name in ["ncs.log", "devel.log", "audit.log", "ncs-java-vm.log", "ncs-python-vm.log"]:
    adapter.ingest_log_file(f"/var/log/ncs/{name}", tail_lines=300)

# 5. Live follow — replaces `tail -F | grep`:
for state in adapter.follow_log_file("/var/log/ncs/ncs.log"):
    notifier.notify_if_anomalies_card(state)
```

Log ingestion maps standard NSO error signals to per-device observations. A device that fails across multiple log scans surfaces as a named anomaly. A single transient error drops off as the window rolls forward.

---

### General Adapters

**`OrchestratorAdapter`** — O→S→H subagent fan-out. Each execution lane logs as machine latency. The synthesis turn logs as generation latency and never alarms.

```python
from agent_logging_system.adapters import OrchestratorAdapter

orch = OrchestratorAdapter(LoggingAgent())
orch.log_fanout(orch.EXECUTION, [("shard-1", 480), ("shard-2", 9000)])
orch.log_synthesis(18000)   # generation latency — never alarms
```

**`WarrantAdapter`** — coding agent instrumentation. Reasoning and generation steps log as generation latency. Citation checks log as machine latency.

**`AimapAdapter`** — feed a completed aimap JSON report into the monitor. Produces per-enumerator observations; enumerators with high error rates surface as anomalies.

```python
from agent_logging_system.adapters import AimapAdapter

adapter = AimapAdapter(LoggingAgent())
state = adapter.ingest_report("/tmp/aimap-report.json")
```

---

## Webex Alerting

`WebexNotifier` forwards anomalies to a Webex room via bot token. Stdlib only — no SDK required.

```python
from agent_logging_system.alerting import WebexNotifier

notifier = WebexNotifier(bot_token="...", room_id="...", min_level="HIGH")

notifier.notify_if_anomalies(state)       # plain text message
notifier.notify_if_anomalies_card(state)  # Adaptive Card with severity colors and recommendations
notifier.send_adaptive_card(card_body)    # raw Adaptive Card body list
```

Setup: create a bot at [developer.webex.com/my-apps/new/bot](https://developer.webex.com/my-apps/new/bot), add it to a Webex space, and pass the bot token and room ID.

---

## Webex Bot — NOC Command Interface

`WebexBotHandler` lets NOC operators query live monitor state directly from a Webex space. The bot replies with Adaptive Cards. Two operating modes:

**Polling mode** — no inbound HTTPS required. The bot calls `GET /v1/messages` on an interval from any machine with outbound internet.

**Webhook mode** — Webex pushes events to your endpoint. Requires an HTTPS-accessible URL (Cloudflare Tunnel, ngrok, or a VPS with TLS termination). Webhook events are verified via HMAC-SHA1 signature.

```python
from agent_logging_system.alerting import WebexBotHandler
from agent_logging_system.alerting.webex_bot import WebexBotPoller, WebexBotServer

handler = WebexBotHandler(monitor, notifier)

# Polling mode:
poller = WebexBotPoller(handler, bot_token="...", room_ids=["room-id"], poll_interval=5.0)
poller.poll_forever()

# Webhook mode:
server = WebexBotServer(handler, host="0.0.0.0", port=8422,
                        bot_token="...", webhook_secret="...")
server.serve_forever()

# Register the webhook once after the HTTPS endpoint is live:
WebexBotServer.register_webhook(bot_token="...", target_url="https://your-domain.com/webhook")
```

**Commands** (send from any Webex client in the space):

| Command | Response |
|---------|---------|
| `als status` | Fleet snapshot — agent count and anomaly summary by level |
| `als anomalies` | All active anomalies with recommendations |
| `als anomalies HIGH` | Anomalies filtered by level (HIGH / MEDIUM / LOW) |
| `als check <agent_id>` | Rolling-window state for a single tool or agent |
| `als recommend` | Full active recommendation list |
| `als help` | Command reference card |

---

## Custom Rules

```python
from agent_logging_system.anomaly_detector import AnomalyRule

monitor.add_anomaly_rule(AnomalyRule(
    name="meraki_result_empty",
    check=lambda s: s.get("error_rate", 0) > 0.25,
    alert_level="HIGH",
    recommendation="Check Meraki organization permissions and API key scope",
))
```

---

## Examples

```bash
# Cisco adapters:
python examples/cisco_thousandeyes.py       # ThousandEyes MCP: wrap_agent, manual log, trace ingest
python examples/cisco_meraki.py             # Meraki Dashboard API: rate limit tracking, 429 anomaly
python examples/cisco_nso.py               # NSO RESTCONF/PyAPI/log: sync-from, commit, log ingestion
python examples/cisco_webex_bot.py         # Webex bot: polling + webhook modes, Adaptive Card responses

# General:
python examples/basic_multi_agent.py        # three agents degrading at different rates
python examples/orchestrator_integration.py # O->S->H fan-out: slow lane alarms, synthesis does not
python examples/self_monitor.py             # the monitor watching itself, with real timing data
```

Run the test suite:

```bash
pytest
```

---

## Use Case

Cisco platforms integrate into AI agent pipelines via MCP servers and REST APIs. When a tool inside those systems degrades — slow responses, rising error rates, 429 throttling, unreachable NSO devices — the pipeline continues silently. The calling agent produces stale or incomplete results. The operator finds out from downstream impact, not from the tool.

Standard logging captures that calls happened. It does not detect that a specific tool is degrading relative to its own baseline, that Meraki rate limit budget is nearly exhausted, or that an NSO `sync-from` is failing consistently on one device.

`agent-logging-system` places a monitor at the adapter layer. Every tool call becomes an `Observation`. The monitor fires named anomalies with concrete remediation steps before the pipeline fails.

---

## Scope

Not a tracer, profiler, or logging framework. No stack frames, no output capture, no external collector, no persistent state. Receives `Observation` structs, evaluates rules over rolling per-agent windows, returns a plain dict. The dict is the API.

---

## Getting Help

Open an issue on [GitHub Issues](https://github.com/sshpie/agent-logging-system/issues) to report bugs, ask questions, or request features. Please include Python version, a minimal reproduction, and any relevant log output.

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on pull requests, code style, and testing requirements.

---

## License

MIT — see [LICENSE](LICENSE). OSI-approved open source license.
