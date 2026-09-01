"""Webex bot acting as a command interface to agent-logging-system.

Two modes shown:

    1. Polling mode   — no inbound HTTPS required; runs anywhere.
    2. Webhook mode   — Webex pushes events; requires an HTTPS endpoint.

Prerequisites:
  1. Create a Webex bot at https://developer.webex.com/my-apps/new/bot
  2. Copy the access token into BOT_TOKEN below.
  3. Add the bot to a Webex space and copy the Room ID into ROOM_ID.

Commands the bot responds to (send from any Webex client in the space):

    als status
    als anomalies
    als anomalies HIGH
    als check <agent_id>
    als recommend
    als help
"""
import time

from agent_logging_system import LoggingAgent, Observation
from agent_logging_system.alerting import WebexNotifier, WebexBotHandler
from agent_logging_system.alerting.webex_bot import WebexBotPoller, WebexBotServer

BOT_TOKEN = "YOUR_BOT_TOKEN"
ROOM_ID   = "YOUR_ROOM_ID"


def _seed_monitor(monitor: LoggingAgent):
    """Seed a few observations so status/anomalies have something to show."""
    ts = "2026-09-01T10:00:00Z"
    for i in range(6):
        monitor.ingest(Observation(
            timestamp=ts,
            agent_id="nso.sync_from.edge-router-01",
            action="sync_from",
            input={"device": "edge-router-01"},
            latency_ms=28000.0,
            status="failed" if i >= 4 else "success",
        ))
    for i in range(5):
        monitor.ingest(Observation(
            timestamp=ts,
            agent_id="meraki.networks.getNetworkDevices",
            action="api_call",
            input={"network": "L_123456"},
            latency_ms=120.0 + i * 40,
            status="success",
        ))


# ── Polling mode ──────────────────────────────────────────────────────────────

def run_polling():
    """Poll for new messages every 5 seconds. No inbound HTTPS needed."""
    monitor  = LoggingAgent()
    notifier = WebexNotifier(bot_token=BOT_TOKEN, room_id=ROOM_ID, min_level="LOW")
    handler  = WebexBotHandler(monitor, notifier)
    poller   = WebexBotPoller(
        bot_handler=handler,
        bot_token=BOT_TOKEN,
        room_ids=[ROOM_ID],
        poll_interval=5.0,
    )

    _seed_monitor(monitor)

    print("Polling for Webex commands (Ctrl-C to stop) ...")
    print("Send 'als status' or 'als help' in the Webex space to test.")
    poller.poll_forever()


# ── Webhook mode ──────────────────────────────────────────────────────────────

def run_webhook():
    """Receive Webex webhook events.

    Requires an inbound HTTPS URL. Options:
      - Cloudflare Tunnel:  cloudflared tunnel --url http://localhost:8422
      - ngrok:              ngrok http 8422
      - VPS with nginx TLS termination

    After the tunnel is up, register the webhook once:

        WebexBotServer.register_webhook(
            bot_token=BOT_TOKEN,
            target_url="https://your-tunnel-domain.com/webhook",
            name="als-bot",
            secret=WEBHOOK_SECRET,   # match what you pass below
        )
    """
    WEBHOOK_SECRET = "replace-with-a-random-string"

    monitor  = LoggingAgent()
    notifier = WebexNotifier(bot_token=BOT_TOKEN, room_id=ROOM_ID, min_level="LOW")
    handler  = WebexBotHandler(monitor, notifier)

    _seed_monitor(monitor)

    server = WebexBotServer(
        bot_handler=handler,
        host="0.0.0.0",
        port=8422,
        bot_token=BOT_TOKEN,
        webhook_secret=WEBHOOK_SECRET,
    )

    print("Webhook server listening on http://0.0.0.0:8422/webhook")
    print("Expose via:  cloudflared tunnel --url http://localhost:8422")
    server.serve_forever()


# ── Standalone command test ───────────────────────────────────────────────────

def test_handler_directly():
    """Exercise the handler without a live bot token.

    WebexNotifier calls will fail (no real token), but the card-building
    and command-routing logic runs and prints the would-be card body.
    """
    import json

    monitor  = LoggingAgent()
    notifier = WebexNotifier(bot_token="test", room_id="test-room", min_level="LOW")
    handler  = WebexBotHandler(monitor, notifier)

    _seed_monitor(monitor)

    # Patch notifier to print instead of calling the API.
    def _print_card(card_body, fallback_text=""):
        print(f"\n── fallback: {fallback_text}")
        print(json.dumps(card_body, indent=2))
        return {}

    notifier.send_adaptive_card = _print_card

    for cmd in [
        "als status",
        "als anomalies",
        "als anomalies HIGH",
        "als check nso.sync_from.edge-router-01",
        "als recommend",
        "als help",
    ]:
        print(f"\n{'─'*60}")
        print(f">> {cmd}")
        handler.handle_message(cmd, "test-room")


if __name__ == "__main__":
    # Choose a mode:
    test_handler_directly()
    # run_polling()
    # run_webhook()
