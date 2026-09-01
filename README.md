<h1 align="center">agent-logging-system</h1>

<div align="center">
<img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+">
<img src="https://img.shields.io/badge/dependencies-none-brightgreen?style=flat-square" alt="No dependencies">
<img src="https://img.shields.io/badge/ingest-O(1)-blue?style=flat-square" alt="O(1) ingest">
<a href="https://github.com/sshpie/agent-logging-system/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/sshpie/agent-logging-system/tests.yml?label=tests&style=flat-square" alt="tests"></a>
<a href="https://github.com/sshpie/agent-logging-system/blob/main/LICENSE"><img src="https://img.shields.io/github/license/sshpie/agent-logging-system?style=flat-square" alt="MIT"></a>
<a href="https://github.com/sshpie"><img src="https://img.shields.io/badge/by-sshpie-blue?style=flat-square" alt="sshpie"></a>
</div>

<br />

<div align="center">
A monitor that watches your AI agents for <strong>slow responses</strong>, <strong>rising error rates</strong>, and <strong>empty outputs</strong> — and tells you which agent is broken and what to do about it.
</div>

---

## Table of Contents

1. [Overview](#overview)
2. [Features](#features)
3. [Architecture](#architecture)
4. [Requirements](#requirements)
5. [Components](#components)
6. [Observation Schema](#observation-schema)
7. [Latency Kinds](#latency-kinds)
8. [Default Rules](#default-rules)
9. [Adapters](#adapters)
10. [Quick Start](#quick-start)
11. [Examples](#examples)
12. [Scope](#scope)

---

## Overview

agent-logging-system tracks a fleet of AI agents with per-action observations, per-agent rolling windows, baseline-relative alarms, and named recommendations. One `Observation` in, a plain dict out. No database, no collector, no dependencies.

The design maps OT/ICS shift-operations discipline onto AI pipelines: watch the trend, not the snapshot, and catch a degrading agent before it corrupts your output.

---

## Features

- **Per-action `Observation` schema** — timestamp, agent_id, action, input, output, latency_ms, status, confidence, latency_kind
- **Rolling 20-observation window** per agent, tracked by `StateModel`
- **Baseline-relative alarms** — each agent compared against its own baseline, not a fixed threshold
- **Two latency kinds** (`machine`, `generation`) — generation latency never trips the machine alarm
- **Three default rules** — `latency_high`, `error_rate_high`, `queue_buildup`
- **Named recommendations** — every alarm maps to a concrete action
- **Custom rules** via `AnomalyRule(name, check, alert_level, recommendation)`
- **O(1) ingest**, incremental scan (~15-27 µs for 5 agents)
- **Stdlib only** — no database, no file on disk, no external collector
- **Cisco MCP adapter** — per-tool rolling windows across ThousandEyes, Webex, Catalyst Center, Nexus Dashboard, SD-WAN, IOS XE, Meraki, NSO
- **Meraki rate limit tracking** — `X-RateLimit-Remaining` parsed per call; MEDIUM at 20%, HIGH on 429
- **NSO log ingestion** — parse `ncs.log`, `devel.log`, `audit.log`, `ncserr.log.*` into observations; supports `tail_lines` and live `follow_log_file()` mode
- **Webex alerting** — Adaptive Cards with severity color-coding via bot token; stdlib only

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
        ├─ NSO RESTCONF / PyAPI ────────► NSOAdapter
        │                                   operation-class latency (sync=generation)
        │                                   ingest_log_file() / follow_log_file()
        │
        └─ Any agent framework ─────────► BaseAdapter (subclass)
                                            emit_observation()
                                                    │
                                                    ▼
                                     LoggingAgent.ingest()       ← O(1)
                                                    │
                                                    ▼
                                              StateModel
                                        rolling window (20 obs/agent)
                                        machine vs generation latency
                                                    │
                                              AnomalyDetector
                                        latency_high / error_rate_high
                                        queue_buildup / custom rules
                                                    │
                                         RecommendationEngine
                                                    │
                                     ┌──────────────┴──────────────┐
                                     ▼                             ▼
                             { agents, anomalies,        WebexNotifier
                               recommendations }         Adaptive Card alert
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

## Components

| Component | Responsibility |
|-----------|----------------|
| `Observation` | Structured unit: timestamp, agent_id, action, input, output, latency_ms, status, confidence, latency_kind |
| `StateModel` | Rolling window (20 obs), error rate, machine vs generation latency counts |
| `AnomalyDetector` | Evaluates threshold rules over state snapshots |
| `RecommendationEngine` | Maps alarm name to concrete action |
| `LoggingAgent` | `ingest` + `get_system_state`; incremental scan |
| `adapters/` | Bind into any host system; Cisco-specific adapters included |
| `alerting/` | `WebexNotifier` — plain text, Markdown, and Adaptive Card alerts |

---

## Observation Schema

```python
Observation(
    timestamp: str,          # ISO8601, e.g. "2026-06-02T14:32:00Z"
    agent_id: str,           # unique agent identifier
    action: str,             # api_call, computation, decision, error, ...
    input: Any,              # input to the action
    output: Any = None,      # output of the action
    latency_ms: float = 0.0, # duration in ms (meaning set by latency_kind)
    status: str = "success", # success | retry | failed | timeout
    confidence: float = 1.0, # 0.0 to 1.0
    error_details: Optional[Dict[str, str]] = None,
    latency_kind: str = "machine",  # "machine" | "generation"
)
```

---

## Latency Kinds

`latency_ms` carries two different quantities, distinguished by `latency_kind`:

| kind | what it is | high means | feeds `latency_high` alarm? |
|------|-----------|-----------|---------------------------|
| `"machine"` (default) | execution time of a call | pathological, contended | yes |
| `"generation"` | wall-clock of a long-running output | expected | no |

A duration tagged `generation` can never trip the machine alarm. `"machine"` is the default.

```python
from agent_logging_system import Observation, LATENCY_GENERATION

Observation(
    timestamp="...", agent_id="explainer-001", action="explain",
    input={...}, output={...}, latency_ms=12000,
    latency_kind=LATENCY_GENERATION,   # 12s is fine; never alarms
)
```

---

## Default Rules

| Rule | Level | Trips when | Recommends |
|------|-------|-----------|-----------|
| `latency_high` | HIGH | machine-kind recent avg exceeds agent's own baseline by 3x, above 100ms floor, after 4-sample warmup | `throttle_input` |
| `error_rate_high` | MEDIUM | error rate over 10% | `investigate_failures` |
| `queue_buildup` | LOW | over 10 observations, error rate under 5% | signal only |

`latency_high` is baseline-relative. A steady-slow agent never trips; a 1000ms-to-9000ms spike does.

---

## Adapters

Subclass `BaseAdapter` to wire any host system into the monitor.

**`OrchestratorAdapter`** — O->S->H subagent fan-out. Each lane logged as machine latency. Synthesis turn logged as generation latency and never alarms.

```python
from agent_logging_system.adapters import OrchestratorAdapter

orch = OrchestratorAdapter(LoggingAgent())
orch.log_fanout(orch.EXECUTION, [("shard 1", 480), ("shard 2", 9000)])
orch.log_synthesis(18000)   # generation, never alarms
```

**`WarrantAdapter`** — coding agent. Reasoning and generation log as generation latency. Citation checks log as machine latency.

**`AimapAdapter`** — feed a completed aimap JSON report into the monitor. Per-enumerator observations; enumerator error rates surface as anomalies.

```python
from agent_logging_system.adapters import AimapAdapter

adapter = AimapAdapter(LoggingAgent())
state = adapter.ingest_report("/tmp/aimap-report.json")
```

---

### Cisco Adapters

**`CiscoMCPAdapter`** — monitor Cisco MCP server tool calls. Each tool tracked as `<product>.<tool_name>`. Three integration paths:

```python
from agent_logging_system.adapters import CiscoMCPAdapter

adapter = CiscoMCPAdapter(LoggingAgent())

# 1. Single call:
adapter.log_tool_call("get_alerts", product=CiscoMCPAdapter.THOUSANDEYES, latency_ms=340)

# 2. Bulk session log:
adapter.ingest_session_log({"product": "thousandeyes", "tool_calls": [...]})

# 3. Real MCP JSON-RPC 2.0 wire trace:
adapter.ingest_mcp_trace(trace_messages, product=CiscoMCPAdapter.THOUSANDEYES)
```

Products: `THOUSANDEYES`, `WEBEX`, `WEBEX_MESSAGING`, `CATALYST_CENTER`, `NEXUS_DASHBOARD`, `CATALYST_SDWAN`, `IOS_XE`, `MERAKI`, `NSO`.

---

**`WebexMessagingMCPAdapter`** — pre-configured for the Webex Messaging MCP Server. All 24 tools pre-classified as read, write, or stream. Wrap an MCP session once and every call is automatically observed.

```python
from agent_logging_system.adapters import WebexMessagingMCPAdapter

adapter = WebexMessagingMCPAdapter(LoggingAgent())
session = adapter.wrap_agent(mcp_client_session)
await session.async_call_tool("webex-create-message", {"roomId": "...", "text": "Hello"})
```

---

**`MerakiAdapter`** — monitor Cisco Meraki Dashboard API calls. Tracks `X-RateLimit-Remaining` on every call. MEDIUM anomaly at 20% remaining; HIGH on 429 — before the next call goes out.

```python
from agent_logging_system.adapters import MerakiAdapter

adapter = MerakiAdapter(LoggingAgent(), organization_id="org-L_123456")

# Context manager: auto-times, handles exceptions.
with adapter.observe("networks.getNetworkDevices") as obs:
    result = dashboard.networks.getNetworkDevices(networkId=net_id)
    obs.rate_limit_remaining = int(resp.headers["X-RateLimit-Remaining"])
    obs.result_count = len(result)

# Manual: log after any HTTP client call.
adapter.log_api_call("devices.getDevice", status_code=200, latency_ms=110, rate_limit_remaining=2)

# Header ingest: extract rate limit data from raw response headers.
adapter.ingest_response_headers("networks.getNetworkDevices", 200, 134.0, resp.headers)
```

---

**`NSOAdapter`** — monitor Cisco NSO RESTCONF/NETCONF/PyAPI operations and log files. Operation-class aware: `sync-from`, `sync-all`, `re-deploy-all` use generation latency (no alarm). `commit` and `check-sync` use machine latency.

```python
from agent_logging_system.adapters import NSOAdapter

adapter = NSOAdapter(LoggingAgent(), nso_host="nso.corp.example.com")

# 1. Context manager:
with adapter.observe("sync_from", device="edge-router-01") as obs:
    result = maapi_session.sync_from("edge-router-01")

# 2. Manual log:
adapter.log_operation("check_sync", device="edge-router-01", status="success", latency_ms=95)
adapter.log_commit(dry_run=True, latency_ms=155.0, changeset_size=12)

# 3. RESTCONF response:
adapter.ingest_restconf_response("/restconf/operations/tailf-ncs:sync-from", "POST", 200, 5200.0)

# 4. Log file ingest — replaces grep triage scripts:
for name in ["ncs.log", "devel.log", "audit.log", "ncs-java-vm.log", "ncs-python-vm.log"]:
    adapter.ingest_log_file(f"/var/log/ncs/{name}", tail_lines=300)

# 5. Live follow — replaces `tail -F | grep`, yields on every new error line:
for state in adapter.follow_log_file("/var/log/ncs/ncs.log"):
    notifier.notify_if_anomalies_card(state)
```

Log ingestion maps the standard NSO error signal patterns to per-device observations. A device that fails across multiple scan runs surfaces as a named anomaly with a recommendation. A single transient error drops off naturally as the window rolls forward.

---

**`WebexNotifier`** — forward anomalies to a Webex room via bot token. Stdlib only.

```python
from agent_logging_system.alerting import WebexNotifier

notifier = WebexNotifier(bot_token="...", room_id="...", min_level="HIGH")

notifier.notify_if_anomalies(state)       # plain text
notifier.notify_if_anomalies_card(state)  # Adaptive Card: severity color, anomaly table, recommendations
```

Setup: create a bot at `developer.webex.com/my-apps/new/bot`, add it to a room, pass the token and room ID.

---

**Custom rules:**

```python
from agent_logging_system.anomaly_detector import AnomalyRule

monitor.add_anomaly_rule(AnomalyRule(
    name="confidence_collapse",
    check=lambda s: s.get("error_rate", 0) > 0.25,
    alert_level="HIGH",
    recommendation="Pause agent and review recent inputs",
))
```

---

## Quick Start

### Step 1 — Install

```bash
pip install agent-logging-system
```

Or from source:

```bash
git clone https://github.com/sshpie/agent-logging-system
cd agent-logging-system
pip install -e .
```

### Step 2 — Instrument an agent

```python
from agent_logging_system import LoggingAgent, Observation

monitor = LoggingAgent()

monitor.ingest(Observation(
    timestamp="2026-06-02T14:32:00Z",
    agent_id="worker-001",
    action="api_call",
    input={"query": "example"},
    output={"result": "ok"},
    latency_ms=1200,
    status="success",
    confidence=0.95,
))
```

### Step 3 — Read the state

```python
state = monitor.get_system_state()
print(state["anomalies"])
print(state["recommendations"])
```

**Output:**

```python
{
    "agents": {
        "worker-001": {
            "agent_id": "worker-001",
            "avg_latency": 1200.0,
            "error_rate": 0.0,
            "total_observations": 1,
            ...
        }
    },
    "anomalies": [],
    "recommendations": []
}
```

---

## Examples

```bash
python examples/basic_multi_agent.py        # three agents degrading at different rates
python examples/warrant_integration.py      # Warrant adapter: reasoning, code, citation logs
python examples/orchestrator_integration.py # O->S->H fan-out: slow lane alarms, synthesis does not
python examples/session_replay.py           # replay a session transcript through the monitor
python examples/self_monitor.py             # the monitor watching itself, with real perf timings
python examples/cisco_thousandeyes.py       # ThousandEyes MCP: wrap_agent, manual log, trace ingest
python examples/cisco_meraki.py             # Meraki Dashboard API: rate limit tracking, 429 anomaly
python examples/cisco_nso.py               # NSO RESTCONF/PyAPI/log: sync-from, commit, log ingestion
```

Run the test suite:

```bash
pytest
```

---

## Scope

Not a tracer, profiler, or logging framework. No stack frames, no output capture, no external collector, no persistent state. Receives `Observation` structs, evaluates rules over rolling per-agent windows, returns a plain dict.

---

## Other Projects

- [aimap](https://github.com/sshpie/aimap) — fingerprint scanner for exposed AI and ML infrastructure
- [BARE](https://github.com/sshpie/BARE) — semantic exploit-module ranking over scanner findings

---

## License

MIT.
