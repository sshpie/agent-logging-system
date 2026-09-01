<h1 align="center">agent-logging-system</h1>

<h4 align="center">A monitor that watches your AI agents for slow responses, rising error rates, and empty outputs — and tells you which agent is broken and what to do about it.</h4>

<p align="center">
  <a href="https://github.com/sshpie/agent-logging-system/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/sshpie/agent-logging-system/tests.yml?label=tests&style=flat-square" alt="tests"></a>
  <a href="https://github.com/sshpie/agent-logging-system/blob/main/LICENSE"><img src="https://img.shields.io/github/license/sshpie/agent-logging-system?style=flat-square" alt="license"></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python" alt="python"></a>
  <a href="https://github.com/sshpie"><img src="https://img.shields.io/badge/by-sshpie-blue?style=flat-square" alt="sshpie"></a>
</p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#installation">Installation</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#components">Components</a> •
  <a href="#default-rules">Rules</a> •
  <a href="#adapters">Adapters</a> •
  <a href="#scope">Scope</a>
</p>

---

One `Observation` per agent action goes in. A rolling per-agent window tracks the trend. An `AnomalyDetector` trips named alarms when behavior drifts from each agent's own baseline. A `RecommendationEngine` maps each alarm to a concrete action. Stdlib only. No dependencies.

# Features

- Per-action `Observation` schema: timestamp, agent_id, action, input, output, latency_ms, status, confidence, latency_kind
- Rolling 20-observation window per agent, tracked by `StateModel`
- Baseline-relative alarms: each agent compared against its own baseline, not a fixed threshold
- Two latency kinds (`machine`, `generation`) — generation latency never trips the machine alarm
- Three default rules: `latency_high`, `error_rate_high`, `queue_buildup`
- `RecommendationEngine` maps every alarm to a named action (`throttle_input`, `investigate_failures`)
- `BaseAdapter` to wire any host system in; Orchestrator and Warrant adapters included
- Custom rules via `AnomalyRule(name, check, alert_level, recommendation)`
- O(1) ingest, incremental scan (~15-27 us for 5 agents)

# Installation

```bash
pip install agent-logging-system
```

Or from source:

```bash
git clone https://github.com/sshpie/agent-logging-system
cd agent-logging-system
pip install -e .             # editable install
pip install -e ".[dev]"      # plus test deps
```

Python 3.9 or later.

# Quick start

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

state = logger.get_system_state()
print(state["anomalies"])
print(state["recommendations"])
```

# Components

| Component | Responsibility |
|-----------|----------------|
| `Observation` | Structured unit a worker emits: timestamp, agent_id, action, input, output, latency_ms, status, confidence, latency_kind |
| `StateModel` | Rolling window (20 obs), trends, error rate, machine vs generation latency counts |
| `AnomalyDetector` | Evaluates threshold rules over state snapshots |
| `RecommendationEngine` | Maps alarm name to concrete action |
| `LoggingAgent` | `ingest` + `get_system_state`; incremental scan |
| `adapters/` | Bind into an orchestrator, coding agent, or custom loop |

# Observation schema

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

# Latency kinds

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

# Default rules

| Rule | Level | Trips when | Recommends |
|------|-------|-----------|-----------|
| `latency_high` | HIGH | machine-kind recent avg exceeds agent's own baseline by 3x, above a 100ms floor, after 4-sample warmup | `throttle_input` |
| `error_rate_high` | MEDIUM | error rate over 10% | `investigate_failures` |
| `queue_buildup` | LOW | over 10 observations and error rate under 5% | signal only |

`latency_high` is baseline-relative. A steady-slow agent never trips; a 1000ms-to-9000ms spike does.

# `get_system_state` output

```python
{
    "agents": {
        "worker-001": {
            "agent_id": "worker-001",
            "status": "in_progress",
            "avg_latency": 1200.0,
            "recent_avg_latency": 1200.0,
            "baseline_latency": 0.0,
            "machine_recent_avg_latency": 1200.0,
            "machine_baseline_latency": 0.0,
            "machine_observations": 1,
            "generation_observations": 0,
            "error_count": 0,
            "error_rate": 0.0,
            "total_observations": 1,
            ...
        }
    },
    "anomalies": [
        {"name": "latency_high", "alert_level": "HIGH",
         "recommendation": "...", "agent_id": "worker-001"}
    ],
    "recommendations": [
        {"action": "throttle_input", "priority": "HIGH",
         "reason": "...", "agent_id": "worker-001"}
    ]
}
```

# Adapters

Subclass `BaseAdapter` to wire a host system into the monitor.

**`OrchestratorAdapter`** for an O->S->H subagent fan-out. Logs per lane (`retrieval.sonnet`, `execution.haiku`) as machine latency. A slow batch trips. The orchestrator's synthesis turn logs as generation latency and never alarms.

```python
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters import OrchestratorAdapter

orch = OrchestratorAdapter(LoggingAgent())
orch.log_fanout(orch.EXECUTION, [("shard 1", 480), ("shard 2", 9000)])  # machine, per lane
orch.log_synthesis(18000)                                               # generation, never alarms
print(orch.get_state()["anomalies"])
```

**`WarrantAdapter`** for a book-grounded coding agent. Reasoning and code generation log as generation latency. Citation checks log as machine latency — a slow lookup is a real signal.

# Custom rules

```python
from agent_logging_system.anomaly_detector import AnomalyRule

logger.add_anomaly_rule(AnomalyRule(
    name="confidence_collapse",
    check=lambda s: s.get("error_rate", 0) > 0.25,
    alert_level="HIGH",
    recommendation="Pause agent and review recent inputs",
))
```

# Performance

`ingest` is O(1). `get_system_state` re-evaluates only agents that changed since the last scan.

| Operation | Cost |
|-----------|------|
| `ingest` | ~2 us |
| `get_system_state` | ~15 to 27 us for 5 agents |

# Tests

```bash
pytest
```

Six test files: logging agent, observation schema, anomaly detector, state model, orchestrator adapter, integration.

# Examples

```bash
python examples/basic_multi_agent.py        # three agents degrading at different rates
python examples/warrant_integration.py      # Warrant adapter: reasoning, code, citation logs
python examples/orchestrator_integration.py # O->S->H fan-out: slow lane alarms, long synthesis does not
python examples/session_replay.py           # replay a session transcript through the monitor
python examples/self_monitor.py             # the monitor watching itself, with real perf timings
```

# Scope

Not a tracer, profiler, or logging framework. No stack frames, no output capture, no external collector, no persistent state. Receives `Observation` structs, evaluates rules over rolling per-agent windows, returns a plain dict.

# Other projects

- [aimap](https://github.com/sshpie/aimap) — fingerprint scanner for exposed AI and ML infrastructure
- [BARE](https://github.com/sshpie/BARE) — semantic exploit-module ranking over scanner findings

# License

MIT. Part of the sshpie toolchain.
