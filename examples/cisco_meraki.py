"""Cisco Meraki Dashboard API monitoring example.

Shows three integration paths for the MerakiAdapter:

  1. Context manager  — auto-times a call, handles exceptions
  2. Manual logging   — log_api_call() after any HTTP client call
  3. Header ingest    — extract rate limit data from raw response headers

Run with Python 3.9+ — no meraki package required for paths 2 and 3.
For path 1 with the official SDK: pip install meraki
"""
from agent_logging_system import LoggingAgent
from agent_logging_system.adapters import MerakiAdapter
from agent_logging_system.alerting import WebexNotifier


# ── PATH 1: context manager (official meraki SDK) ─────────────────────────────

def monitor_with_sdk(dashboard, net_id: str):
    """Wrap Meraki SDK calls with the observe() context manager.

    Requires `pip install meraki` and a real DashboardAPI instance.
    """
    monitor = LoggingAgent()
    adapter = MerakiAdapter(monitor, organization_id="your-org-id")

    # Auto-timed; exception -> "failed" observation + re-raise.
    with adapter.observe("networks.getNetworkDevices") as obs:
        result = dashboard.networks.getNetworkDevices(networkId=net_id)
        obs.result_count = len(result)
        # If your SDK wrapper exposes headers, pass the remaining limit:
        # obs.rate_limit_remaining = int(last_headers["X-RateLimit-Remaining"])

    with adapter.observe("networks.getNetworkClients") as obs:
        clients = dashboard.networks.getNetworkClients(networkId=net_id, perPage=100)
        obs.result_count = len(clients)
        obs.is_paginated = True   # suppresses latency alarm on pagination chains

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── PATH 2: manual logging ────────────────────────────────────────────────────

def monitor_with_manual_logging():
    """Log Meraki API calls manually — works with any HTTP client."""
    monitor = LoggingAgent()
    adapter = MerakiAdapter(monitor, organization_id="org-L_123456")

    # Normal calls with rate limit context from X-RateLimit-Remaining header.
    calls = [
        ("organizations.getOrganization",     200, 98,  45.0),
        ("networks.getNetwork",               200, 96,  52.0),
        ("networks.getNetworkDevices",        200, 94, 134.0),
        ("networks.getNetworkClients",        200, 90, 220.0),
        ("wireless.getNetworkWirelessStatus", 200, 88,  91.0),
        # Simulate approaching rate limit.
        ("devices.getDevice",                 200,  2, 110.0),  # MEDIUM anomaly: 2/5 remaining
    ]
    for endpoint, status_code, remaining, latency_ms in calls:
        adapter.log_api_call(
            endpoint=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
            rate_limit_remaining=remaining,
            rate_limit_limit=5,
        )

    # Simulate a 429 — immediately HIGH anomaly regardless of window.
    adapter.log_rate_limit_hit("switch.getNetworkSwitchAccessPolicies", retry_after_s=1.0)

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── PATH 3: header ingest (raw urllib/requests response) ─────────────────────

def monitor_with_response_headers():
    """Extract Meraki rate limit headers from a raw HTTP response."""
    monitor = LoggingAgent()
    adapter = MerakiAdapter(monitor, organization_id="org-456")

    # Simulate headers from a response — these come from resp.headers in requests
    # or dict(resp.getheaders()) in urllib.
    sample_headers = {
        "X-RateLimit-Limit":     "5",
        "X-RateLimit-Remaining": "1",
        "Content-Type":          "application/json",
    }

    adapter.ingest_response_headers(
        endpoint="appliance.getNetworkApplianceFirewallInboundRules",
        method="GET",
        status_code=200,
        latency_ms=178.0,
        headers=sample_headers,
    )

    # Simulate a 429 with Retry-After.
    throttled_headers = {
        "X-RateLimit-Limit":     "5",
        "X-RateLimit-Remaining": "0",
        "Retry-After":           "1",
    }
    adapter.ingest_response_headers(
        endpoint="networks.getNetworkDevices",
        method="GET",
        status_code=429,
        latency_ms=22.0,
        headers=throttled_headers,
    )

    state = monitor.get_system_state()
    _print_state(state)
    return state


# ── Webex Adaptive Card alert ─────────────────────────────────────────────────

def alert_to_webex_card(state, bot_token: str, room_id: str):
    """Send an Adaptive Card alert for HIGH anomalies to a Webex room."""
    notifier = WebexNotifier(bot_token=bot_token, room_id=room_id, min_level="HIGH")
    result = notifier.notify_if_anomalies_card(state)
    if result:
        print(f"Adaptive Card posted: {result.get('id')}")
    else:
        print("No HIGH anomalies — no card sent")


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

    print("\n=== PATH 3: header ingest ===")
    monitor_with_response_headers()
