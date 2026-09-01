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
- **Two latency kinds** (`machine`, `generation`) — generation latency never trips the machine alarm, regardless of magnitude
- **Three default rules** — `latency_high`, `error_rate_high`, `queue_buildup`
- **Named recommendations** — every alarm maps to a concrete action (`throttle_input`, `investigate_failures`)
- **`BaseAdapter`** — wire any host system in; Orchestrator and Warrant adapters included
- **Custom rules** via `AnomalyRule(name, check, alert_level, recommendation)`
- **O(1) ingest**, incremental scan (~15-27 µs for 5 agents)
- **Stdlib only** — no database, no file on disk, no external collector

---

## Architecture

```
Worker agents emit Observation structs
            │
            ▼
  LoggingAgent.ingest()       ← O(1): update StateModel, mark agent dirty
            │
            ▼
       StateModel
       ├─ rolling window (20 obs/agent)
       ├─ machine latency series
       └─ generation latency series
            │
            ▼  (on get_system_state())
     AnomalyDetector          ← evaluates only dirty agents
     ├─ latency_high
     ├─ error_rate_high
     └─ queue_buildup
            │
            ▼
  RecommendationEngine
            │
            ▼
  { agents, anomalies, recommendations }
```

**Adapter layer** — bind any host system into the monitor:

```
Host system (orchestrator / coding agent / custom loop)
            │
            ▼
  BaseAdapter.emit_observation()
            │
            ▼
       LoggingAgent
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
| `Observation` | Structured unit a worker emits: timestamp, agent_id, action, input, output, latency_ms, status, confidence, latency_kind |
| `StateModel` | Rolling window (20 obs), trends, error rate, machine vs generation latency counts |
| `AnomalyDetector` | Evaluates threshold rules over state snapshots |
| `RecommendationEngine` | Maps alarm name to concrete action |
| `LoggingAgent` | `ingest` + `get_system_state`; incremental scan |
| `adapters/` | Bind into an orchestrator, coding agent, or custom loop |

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
| `"generation"` | wall-clock of producing a large output | expected | no |

A duration tagged `generation` can never trip the machine alarm. `"machine"` is the default — an unclassified duration is treated as alarmable.

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
| `latency_high` | HIGH | machine-kind recent avg exceeds agent's own baseline by 3x, above a 100ms floor, after 4-sample warmup | `throttle_input` |
| `error_rate_high` | MEDIUM | error rate over 10% | `investigate_failures` |
| `queue_buildup` | LOW | over 10 observations and error rate under 5% | signal only |

`latency_high` is baseline-relative. A steady-slow agent never trips; a 1000ms-to-9000ms spike does.

---

## Adapters

Subclass `BaseAdapter` to wire a host system into the monitor.

**`OrchestratorAdapter`** — O->S->H subagent fan-out. Logs per lane (`retrieval.sonnet`, `execution.haiku`) as machine latency. A slow batch trips. The orchestrator's synthesis turn logs as generation latency and never alarms.

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters import OrchestratorAdapter

orch = OrchestratorAdapter(LoggingAgent())
orch.log_fanout(orch.EXECUTION, [("shard 1", 480), ("shard 2", 9000)])  # machine, per lane
orch.log_synthesis(18000)                                               # generation, never alarms
print(orch.get_state()["anomalies"])
```

**`WarrantAdapter`** — book-grounded coding agent. Reasoning and code generation log as generation latency. Citation checks log as machine latency — a slow lookup is a real signal.

**`AimapAdapter`** — feed a completed aimap JSON report into the monitor. Translates the three aimap phases (port discovery, fingerprint, per-enumerator enum) into observations attributed per enumerator, so per-enumerator error rates are visible.

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters.aimap_adapter import AimapAdapter

monitor = LoggingAgent()
adapter = AimapAdapter(monitor)
state = adapter.ingest_report("/tmp/aimap-report.json")
print(state["anomalies"])
```

**Custom rules:**

```python
from agent_logging_system.anomaly_detector import AnomalyRule

logger.add_anomaly_rule(AnomalyRule(
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

logger = LoggingAgent()

logger.ingest(Observation(
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
state = logger.get_system_state()
print(state["anomalies"])
print(state["recommendations"])
```

**Output:**

```python
{
    "agents": {
        "worker-001": {
            "agent_id": "worker-001",
            "status": "in_progress",
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
python examples/orchestrator_integration.py # O->S->H fan-out: slow lane alarms, long synthesis does not
python examples/session_replay.py           # replay a session transcript through the monitor
python examples/self_monitor.py             # the monitor watching itself, with real perf timings
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
