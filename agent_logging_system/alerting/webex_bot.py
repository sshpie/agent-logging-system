"""Webex bot: command interface to agent-logging-system.

Lets NOC operators query the monitor from any Webex space. Two modes:

    Webhook mode  — Webex POSTs to your HTTPS endpoint on each message.
                    Requires an inbound-HTTPS URL (VPS, Cloudflare Tunnel, ngrok).

    Polling mode  — Bot calls GET /v1/messages on an interval.
                    No inbound HTTPS needed. Runs from any machine with
                    outbound internet.

Commands (send in any Webex space where the bot is a member):

    als status               Fleet snapshot — agent count + anomaly summary
    als anomalies            All active anomalies
    als anomalies HIGH       Anomalies at HIGH (or MEDIUM, LOW)
    als check <agent_id>     Single agent rolling-window state
    als recommend            Active recommendations
    als help                 Command reference

Webhook setup (one-time, via Webex API or developer portal):

    POST https://webexapis.com/v1/webhooks
    {
        "name": "als-bot",
        "targetUrl": "https://your-domain.com/webhook",
        "resource": "messages",
        "event": "created",
        "secret": "your-webhook-secret"   # optional but recommended
    }

Quick start:

    from agent_logging_system import LoggingAgent
    from agent_logging_system.alerting import WebexNotifier, WebexBotHandler
    from agent_logging_system.alerting.webex_bot import WebexBotServer, WebexBotPoller

    monitor = LoggingAgent()
    notifier = WebexNotifier(bot_token="...", room_id="...")
    handler  = WebexBotHandler(monitor, notifier)

    # Webhook mode:
    server = WebexBotServer(handler, host="0.0.0.0", port=8422,
                            bot_token="...", webhook_secret="...")
    server.serve_forever()

    # Polling mode (no HTTPS needed):
    poller = WebexBotPoller(handler, bot_token="...", room_ids=["room-id-1"])
    poller.poll_forever()
"""
import hashlib
import hmac
import http.server
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.alerting.webex_notifier import WebexNotifier

_WEBEX_API = "https://webexapis.com/v1"
_LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_LEVEL_COLOR = {"HIGH": "attention", "MEDIUM": "warning", "LOW": "default"}


# ── Adaptive Card builders ────────────────────────────────────────────────────

def _header(text: str, color: str = "default") -> Dict:
    return {"type": "TextBlock", "text": text, "weight": "Bolder",
            "size": "Medium", "color": color}


def _fact_set(facts: List[tuple]) -> Dict:
    return {"type": "FactSet",
            "facts": [{"title": k, "value": str(v)} for k, v in facts]}


def _text(text: str, subtle: bool = False, color: str = "default") -> Dict:
    block = {"type": "TextBlock", "text": text, "wrap": True}
    if subtle:
        block["isSubtle"] = True
    if color != "default":
        block["color"] = color
    return block


def _divider() -> Dict:
    return {"type": "TextBlock", "text": "─" * 32, "isSubtle": True, "size": "Small"}


def _card_status(state: Dict) -> List[Dict]:
    agents = state.get("agents", {})
    anomalies = state.get("anomalies", [])
    by_level = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for a in anomalies:
        lv = a.get("alert_level", "LOW")
        by_level[lv] = by_level.get(lv, 0) + 1

    max_level = "default"
    if by_level["HIGH"]:
        max_level = "attention"
    elif by_level["MEDIUM"]:
        max_level = "warning"

    body = [
        _header("agent-logging-system — Fleet Status", color=max_level),
        _fact_set([
            ("Agents tracked", len(agents)),
            ("HIGH",    by_level["HIGH"]),
            ("MEDIUM",  by_level["MEDIUM"]),
            ("LOW",     by_level["LOW"]),
        ]),
    ]

    if anomalies:
        body.append(_divider())
        body.append(_text("Active anomalies", subtle=True))
        for a in anomalies[:8]:   # cap card length
            lv = a.get("alert_level", "LOW")
            body.append(_text(
                f"[{lv}]  {a.get('agent_id', '?')}  —  {a.get('name', '?')}",
                color=_LEVEL_COLOR.get(lv, "default"),
            ))
    else:
        body.append(_text("No anomalies.", subtle=True))

    return body


def _card_anomalies(anomalies: List[Dict]) -> List[Dict]:
    if not anomalies:
        return [_header("Anomalies"), _text("None.", subtle=True)]

    body = [_header(f"Anomalies ({len(anomalies)})")]
    for a in anomalies:
        lv = a.get("alert_level", "LOW")
        body.append(_divider())
        body.append(_text(
            f"[{lv}]  {a.get('agent_id', '?')}",
            color=_LEVEL_COLOR.get(lv, "default"),
        ))
        body.append(_text(f"Rule: {a.get('name', '?')}", subtle=True))
        rec = a.get("recommendation", "")
        if rec:
            body.append(_text(f"→ {rec}", subtle=True))
    return body


def _card_check(agent_id: str, agent_state: Optional[Dict],
                anomalies: List[Dict]) -> List[Dict]:
    if agent_state is None:
        return [
            _header(f"check: {agent_id}"),
            _text("Agent not found. No observations recorded.", subtle=True),
        ]

    avg_lat = agent_state.get("avg_latency", 0.0)
    err_rate = agent_state.get("error_rate", 0.0)
    total = agent_state.get("total_observations", 0)
    mach = agent_state.get("machine_observations", 0)
    gen = agent_state.get("generation_observations", 0)

    agent_anomalies = [a for a in anomalies if a.get("agent_id") == agent_id]
    color = _LEVEL_COLOR.get(
        agent_anomalies[0].get("alert_level", "LOW") if agent_anomalies else "LOW",
        "default",
    ) if agent_anomalies else "default"

    body = [
        _header(agent_id, color=color),
        _fact_set([
            ("avg latency",  f"{avg_lat:.0f} ms"),
            ("error rate",   f"{err_rate:.0%}"),
            ("observations", total),
            ("machine",      mach),
            ("generation",   gen),
        ]),
    ]

    if agent_anomalies:
        body.append(_divider())
        for a in agent_anomalies:
            lv = a.get("alert_level", "LOW")
            body.append(_text(
                f"[{lv}] {a.get('name', '?')}",
                color=_LEVEL_COLOR.get(lv, "default"),
            ))
            if a.get("recommendation"):
                body.append(_text(f"→ {a['recommendation']}", subtle=True))
    return body


def _card_recommend(recommendations: List[Dict]) -> List[Dict]:
    if not recommendations:
        return [_header("Recommendations"), _text("None.", subtle=True)]

    body = [_header(f"Recommendations ({len(recommendations)})")]
    for r in recommendations:
        body.append(_divider())
        body.append(_text(r.get("agent_id", "?"), color="accent"))
        body.append(_text(r.get("action", "?"), subtle=True))
    return body


def _card_help() -> List[Dict]:
    return [
        _header("agent-logging-system commands"),
        _fact_set([
            ("als status",              "Fleet snapshot"),
            ("als anomalies",           "All anomalies"),
            ("als anomalies HIGH",      "Filter by level (HIGH/MEDIUM/LOW)"),
            ("als check <agent_id>",    "Single agent state"),
            ("als recommend",           "Active recommendations"),
            ("als help",                "This message"),
        ]),
    ]


# ── Command parser ────────────────────────────────────────────────────────────

def _parse_command(text: str):
    """Strip @mentions and extract the command + args after 'als'."""
    # Remove @mentions (format: <spark-mention data-object-type=...>Name</spark-mention>)
    import re
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip().lower()

    # Remove leading 'als' prefix if present.
    parts = text.split()
    if parts and parts[0] == "als":
        parts = parts[1:]

    cmd = parts[0] if parts else "help"
    args = parts[1:] if len(parts) > 1 else []
    return cmd, args


# ── Core handler ─────────────────────────────────────────────────────────────

class WebexBotHandler:
    """Parse Webex bot commands and respond with monitor state.

    Shared between webhook and polling modes — neither depends on HTTP.

    Args:
        monitor:  The LoggingAgent to query.
        notifier: WebexNotifier used for Adaptive Card responses.
                  Its room_id is overridden per-response (the reply goes to
                  the same room the command came from).
    """

    def __init__(self, monitor: LoggingAgent, notifier: WebexNotifier):
        self._monitor = monitor
        self._notifier = notifier

    def handle_message(self, text: str, room_id: str) -> Optional[Dict]:
        """Parse a command and send an Adaptive Card response.

        Args:
            text:    Raw message text from Webex (may contain HTML @mentions).
            room_id: Webex room ID to reply to.

        Returns the Webex API response dict, or None if the command was
        unrecognized and a help card was sent.
        """
        cmd, args = _parse_command(text)
        state = self._monitor.get_system_state()

        if cmd == "status":
            card_body = _card_status(state)
            fallback = f"Fleet: {len(state['agents'])} agents, {len(state['anomalies'])} anomalies"

        elif cmd == "anomalies":
            threshold = args[0].upper() if args else "LOW"
            filtered = [
                a for a in state.get("anomalies", [])
                if _LEVEL_ORDER.get(a.get("alert_level", "LOW"), 0)
                   >= _LEVEL_ORDER.get(threshold, 0)
            ]
            card_body = _card_anomalies(filtered)
            fallback = f"{len(filtered)} anomalies at {threshold}+"

        elif cmd == "check":
            agent_id = args[0] if args else ""
            agent_state = state.get("agents", {}).get(agent_id)
            card_body = _card_check(agent_id, agent_state, state.get("anomalies", []))
            fallback = f"check: {agent_id}"

        elif cmd in ("recommend", "recommendations"):
            card_body = _card_recommend(state.get("recommendations", []))
            fallback = f"{len(state.get('recommendations', []))} recommendations"

        else:
            card_body = _card_help()
            fallback = "agent-logging-system command reference"

        # Temporarily override notifier room_id to reply to the correct space.
        orig_room = self._notifier._room_id
        self._notifier._room_id = room_id
        try:
            return self._notifier.send_adaptive_card(card_body, fallback_text=fallback)
        finally:
            self._notifier._room_id = orig_room


# ── Webhook mode ─────────────────────────────────────────────────────────────

class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for Webex webhook events."""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        # Verify HMAC-SHA1 signature if a secret is configured.
        secret = self.server.webhook_secret
        if secret:
            sig_header = self.headers.get("X-Spark-Signature", "")
            expected = hmac.new(
                secret.encode(), raw_body, hashlib.sha1
            ).hexdigest()
            if not hmac.compare_digest(expected, sig_header):
                self._respond(401)
                return

        try:
            event = json.loads(raw_body)
        except json.JSONDecodeError:
            self._respond(400)
            return

        # Acknowledge immediately — Webex expects a fast 200.
        self._respond(200)

        # Fetch the full message content (webhook only gives us the ID).
        resource = event.get("resource", "")
        data = event.get("data", {})
        if resource != "messages":
            return

        msg_id = data.get("id", "")
        room_id = data.get("roomId", "")
        actor_id = data.get("personId", "")

        if not msg_id or not room_id:
            return

        # Don't reply to the bot's own messages.
        if actor_id == self.server.bot_person_id:
            return

        msg = _fetch_message(msg_id, self.server.bot_token)
        if not msg:
            return

        text = msg.get("text", "") or msg.get("html", "")
        self.server.bot_handler.handle_message(text, room_id)

    def _respond(self, code: int):
        self.send_response(code)
        self.end_headers()


class WebexBotServer(http.server.HTTPServer):
    """Receive Webex webhook events and dispatch to a WebexBotHandler.

    Requires an inbound HTTPS URL. Webex will not POST to plain HTTP.
    Options: Cloudflare Tunnel (cloudflared tunnel), ngrok, or a VPS
    with nginx TLS termination pointing to this server.

    Args:
        bot_handler:     A WebexBotHandler instance.
        host:            Bind address.
        port:            TCP port (default 8422).
        bot_token:       The Webex bot access token.
        webhook_secret:  Secret used to verify X-Spark-Signature headers.
                         Leave "" to skip verification (development only).
    """

    def __init__(
        self,
        bot_handler: WebexBotHandler,
        host: str = "127.0.0.1",
        port: int = 8422,
        bot_token: str = "",
        webhook_secret: str = "",
    ):
        super().__init__((host, port), _WebhookHandler)
        self.bot_handler = bot_handler
        self.bot_token = bot_token
        self.webhook_secret = webhook_secret
        self.bot_person_id = _get_bot_person_id(bot_token)

    @classmethod
    def register_webhook(cls, bot_token: str, target_url: str,
                         name: str = "als-bot", secret: str = "") -> Dict:
        """Register this server as a Webex webhook.

        Call once after starting the server. `target_url` must be HTTPS-accessible.

        Returns the Webex API response (includes the webhook ID).
        """
        payload = {
            "name": name,
            "targetUrl": target_url,
            "resource": "messages",
            "event": "created",
        }
        if secret:
            payload["secret"] = secret

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_WEBEX_API}/webhooks",
            data=data,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())


# ── Polling mode ─────────────────────────────────────────────────────────────

class WebexBotPoller:
    """Poll Webex for new messages and dispatch to a WebexBotHandler.

    No inbound HTTPS required — all calls are outbound to the Webex API.
    Tracks the last-seen message timestamp per room so it only processes
    new messages on each poll.

    Args:
        bot_handler:    A WebexBotHandler instance.
        bot_token:      The Webex bot access token.
        room_ids:       List of Webex room IDs to poll.
        poll_interval:  Seconds between polls (default 5.0).
    """

    def __init__(
        self,
        bot_handler: WebexBotHandler,
        bot_token: str,
        room_ids: List[str],
        poll_interval: float = 5.0,
    ):
        self._handler = bot_handler
        self._token = bot_token
        self._room_ids = list(room_ids)
        self._interval = poll_interval
        self._last_seen: Dict[str, str] = {}   # room_id → last message id
        self._bot_person_id = _get_bot_person_id(bot_token)

    def poll_once(self) -> int:
        """Poll all rooms once. Returns the number of commands handled."""
        handled = 0
        for room_id in self._room_ids:
            handled += self._poll_room(room_id)
        return handled

    def poll_forever(self):
        """Block and poll continuously. Ctrl-C to stop."""
        while True:
            try:
                self.poll_once()
            except Exception:
                pass
            time.sleep(self._interval)

    def _poll_room(self, room_id: str) -> int:
        messages = _list_messages(room_id, self._token, max_results=10)
        if not messages:
            return 0

        last_id = self._last_seen.get(room_id)
        new_messages = []

        for msg in messages:
            if msg.get("id") == last_id:
                break
            new_messages.append(msg)

        if messages:
            self._last_seen[room_id] = messages[0].get("id", "")

        handled = 0
        for msg in reversed(new_messages):
            if msg.get("personId") == self._bot_person_id:
                continue
            text = msg.get("text", "") or msg.get("html", "")
            if not text:
                continue
            self._handler.handle_message(text, room_id)
            handled += 1

        return handled


# ── Webex API helpers (stdlib only) ──────────────────────────────────────────

def _get(path: str, token: str) -> Optional[Dict]:
    req = urllib.request.Request(
        f"{_WEBEX_API}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        return None


def _fetch_message(msg_id: str, token: str) -> Optional[Dict]:
    return _get(f"/messages/{msg_id}", token)


def _list_messages(room_id: str, token: str, max_results: int = 10) -> List[Dict]:
    data = _get(f"/messages?roomId={room_id}&max={max_results}", token)
    return (data or {}).get("items", [])


def _get_bot_person_id(token: str) -> str:
    """Fetch the bot's own personId so it doesn't reply to itself."""
    data = _get("/people/me", token)
    return (data or {}).get("id", "")
