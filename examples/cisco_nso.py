"""Cisco NSO (Crosswork Network Services Orchestrator) monitoring example.

Shows three integration paths for the NSOAdapter:

  1. Context manager  — auto-times an NSO operation, handles exceptions
  2. Manual logging   — log_operation() for any NSO API client
  3. RESTCONF ingest  — translate raw HTTP response → observation

Covers key NSO operation classes:
  - Device sync (sync-from, check-sync) — medium latency expected
  - Commit / dry-run — fast, failure = high impact
  - Service deploy  — variable, scoped per service instance

Run with Python 3.9+ — no ncs package required.
"""
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters import NSOAdapter
from agent_logging_system.alerting import WebexNotifier


# ── PATH 1: context manager (works with ncs.maapi or any client) ──────────────

def monitor_with_context_manager(maapi_session):
    """Wrap NSO PyAPI calls with the observe() context manager.

    maapi_session: an ncs.maapi.Maapi() session or any object with NSO methods.
    """
    monitor = LoggingAgent()
    adapter = NSOAdapter(monitor, nso_host="nso.corp.example.com")

    with adapter.observe("sync_from", device="edge-router-01") as obs:
        result = maapi_session.sync_from("edge-router-01")
        obs.result = result

    with adapter.observe("commit_dry_run") as obs:
        result = maapi_session.commit_params()
        obs.result = {"changeset": len(result)}

    with adapter.observe("commit") as obs:
        maapi_session.apply()

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── PATH 2: manual logging ────────────────────────────────────────────────────

def monitor_with_manual_logging():
    """Log NSO operations manually — works with RESTCONF urllib or ncs CLI."""
    monitor = LoggingAgent()
    adapter = NSOAdapter(monitor, nso_host="nso.corp.example.com")

    # Device sync operations — medium latency expected.
    ops = [
        ("check_sync",    "edge-router-01", "", "success",  95.0),
        ("check_sync",    "edge-router-02", "", "success", 102.0),
        ("sync_from",     "edge-router-01", "", "success", 4200.0),  # expected slow
        ("commit_dry_run",             "", "", "success",  180.0),
        ("commit",                     "", "", "success",  310.0),
        # Service deploy — scoped by service path.
        ("deploy_service", "", "/ncs:services/vpn[name='corp-vpn-01']", "success", 2800.0),
        # Simulate a failed sync — device unreachable.
        ("sync_from",     "edge-router-03", "", "failed",  30000.0),
    ]

    for op, device, service, status, latency_ms in ops:
        adapter.log_operation(
            operation=op,
            device=device,
            service=service,
            status=status,
            latency_ms=latency_ms,
        )

    # Convenience: log commit/dry-run with changeset size context.
    adapter.log_commit(dry_run=True,  latency_ms=155.0, changeset_size=12)
    adapter.log_commit(dry_run=False, latency_ms=290.0, changeset_size=12)

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── PATH 3: RESTCONF response ingest ─────────────────────────────────────────

def monitor_with_restconf():
    """Translate RESTCONF HTTP responses into NSO observations."""
    monitor = LoggingAgent()
    adapter = NSOAdapter(monitor, nso_host="nso.corp.example.com")

    # POST /restconf/operations/tailf-ncs:sync-from
    adapter.ingest_restconf_response(
        path="/restconf/operations/tailf-ncs:sync-from",
        method="POST",
        status_code=200,
        latency_ms=5200.0,
        body={"sync-from-result": [
            {"device": "edge-router-01", "result": True},
            {"device": "edge-router-02", "result": True},
        ]},
    )

    # GET /restconf/data/tailf-ncs:devices/device=edge-router-01/config
    adapter.ingest_restconf_response(
        path="/restconf/data/tailf-ncs:devices/device=edge-router-01/config",
        method="GET",
        status_code=200,
        latency_ms=320.0,
    )

    # POST /restconf/operations/tailf-ncs:check-sync — device unreachable
    adapter.ingest_restconf_response(
        path="/restconf/operations/tailf-ncs:check-sync",
        method="POST",
        status_code=500,
        latency_ms=30100.0,
        body={"errors": {"error": [{"error-message": "Failed to connect to device"}]}},
    )

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── Webex alerting ────────────────────────────────────────────────────────────

def alert_to_webex(state, bot_token: str, room_id: str):
    notifier = WebexNotifier(bot_token=bot_token, room_id=room_id, min_level="MEDIUM")
    result = notifier.notify_if_anomalies(state)
    if result:
        print(f"Alert posted to Webex: {result.get('id')}")
    else:
        print("No qualifying anomalies — no alert sent")


def _print_state(state):
    print(f"Agents tracked: {len(state['agents'])}")
    for agent_id, data in state["agents"].items():
        print(f"  {agent_id}: avg={data['avg_latency']:.0f}ms errors={data['error_rate']:.0%}")
    if state["anomalies"]:
        print("Anomalies:")
        for a in state["anomalies"]:
            print(f"  [{a['alert_level']}] {a['agent_id']}: {a['name']}")
            if a.get("recommendation"):
                print(f"         -> {a['recommendation']}")
    else:
        print("No anomalies")


if __name__ == "__main__":
    print("=== PATH 2: manual logging ===")
    monitor_with_manual_logging()

    print("\n=== PATH 3: RESTCONF ingest ===")
    monitor_with_restconf()
