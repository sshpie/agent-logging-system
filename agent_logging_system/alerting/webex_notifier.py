"""Webex notifier: forward monitor anomalies to a Webex room via bot token.

Stdlib only — uses urllib. No requests, no webex-sdk dependency.

Setup:
    1. Create a bot at https://developer.webex.com/my-apps/new/bot
    2. Copy the bot access token
    3. Add the bot to a Webex room/space
    4. Get the room ID (see get_room_id() below)

Usage:

    from agent_logging_system import LoggingAgent
    from agent_logging_system.alerting import WebexNotifier

    monitor = LoggingAgent()
    notifier = WebexNotifier(
        bot_token="your-bot-token",
        room_id="your-room-id",
    )

    # After each monitoring cycle:
    state = monitor.get_system_state()
    notifier.notify_if_anomalies(state)
"""
import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

_WEBEX_MESSAGES_URL = "https://webexapis.com/v1/messages"
_WEBEX_ROOMS_URL = "https://webexapis.com/v1/rooms"

_LEVEL_EMOJI = {
    "HIGH": "[HIGH]",
    "MEDIUM": "[MED]",
    "LOW": "[LOW]",
}


class WebexNotifier:
    """Post agent-logging-system anomaly alerts to a Webex room.

    Args:
        bot_token: Webex bot access token from developer.webex.com/my-apps/new/bot
        room_id:   Webex room/space ID. Get with get_room_id() below.
        min_level: Only notify on anomalies at or above this level.
                   "LOW" | "MEDIUM" | "HIGH". Defaults to "MEDIUM".
    """

    _LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

    def __init__(
        self,
        bot_token: str,
        room_id: str,
        min_level: str = "MEDIUM",
    ):
        self._token = bot_token
        self._room_id = room_id
        self._min_level = min_level

    def notify_if_anomalies(self, state: Dict[str, Any]) -> Optional[Dict]:
        """Send a message if state contains anomalies at or above min_level.

        Returns the Webex API response dict, or None if no message was sent.
        """
        anomalies = [
            a for a in (state.get("anomalies") or [])
            if self._meets_threshold(a.get("alert_level", "LOW"))
        ]
        if not anomalies:
            return None
        return self.send_message(self._format_alert(anomalies, state))

    def send_message(self, text: str) -> Dict:
        """Post a plain-text message to the configured room.

        Raises urllib.error.HTTPError on API failure.
        """
        payload = json.dumps({"roomId": self._room_id, "text": text}).encode()
        req = urllib.request.Request(
            _WEBEX_MESSAGES_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def send_markdown(self, markdown: str) -> Dict:
        """Post a Markdown-formatted message to the configured room."""
        payload = json.dumps({"roomId": self._room_id, "markdown": markdown}).encode()
        req = urllib.request.Request(
            _WEBEX_MESSAGES_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    @classmethod
    def get_room_id(cls, bot_token: str, room_name_contains: str = "") -> List[Dict]:
        """List Webex rooms the bot belongs to; filter by partial room name.

        Returns a list of {"id": ..., "title": ...} dicts.
        Useful for finding the room_id to pass to the constructor.

        Example:
            rooms = WebexNotifier.get_room_id("my-bot-token", "ops-alerts")
            print(rooms[0]["id"])
        """
        req = urllib.request.Request(
            _WEBEX_ROOMS_URL,
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        rooms = data.get("items", [])
        if room_name_contains:
            rooms = [r for r in rooms if room_name_contains.lower() in r.get("title", "").lower()]
        return [{"id": r["id"], "title": r.get("title", "")} for r in rooms]

    def send_adaptive_card(self, card_body: List[Dict], fallback_text: str = "") -> Dict:
        """Post a Webex Adaptive Card to the configured room.

        card_body is the list of AdaptiveCard body elements (dicts). The card
        schema version is pinned to 1.4 (universally supported by Webex clients).

        Fallback text is shown in notification previews and clients that do not
        render cards.

        Example card_body element:
            {"type": "TextBlock", "text": "Alert: error_rate_high", "weight": "Bolder"}

        Returns the Webex API response dict.
        """
        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": card_body,
        }
        payload = json.dumps({
            "roomId": self._room_id,
            "text": fallback_text or "agent-logging-system alert",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        }).encode()
        req = urllib.request.Request(
            _WEBEX_MESSAGES_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def notify_if_anomalies_card(self, state: Dict[str, Any]) -> Optional[Dict]:
        """Send an Adaptive Card alert if state contains anomalies at or above min_level.

        Renders a structured card with a severity header, anomaly table, and
        recommendations. Falls back to plain text on clients that don't support cards.

        Returns the Webex API response dict, or None if no anomalies qualify.
        """
        anomalies = [
            a for a in (state.get("anomalies") or [])
            if self._meets_threshold(a.get("alert_level", "LOW"))
        ]
        if not anomalies:
            return None

        max_level = max(
            (a.get("alert_level", "LOW") for a in anomalies),
            key=lambda lvl: self._LEVEL_ORDER.get(lvl, 0),
        )
        color_map = {"HIGH": "attention", "MEDIUM": "warning", "LOW": "default"}
        header_color = color_map.get(max_level, "default")

        card_body: List[Dict] = [
            {
                "type": "TextBlock",
                "text": f"agent-logging-system — {len(anomalies)} anomaly(s)",
                "weight": "Bolder",
                "size": "Medium",
                "color": header_color,
            },
            {
                "type": "ColumnSet",
                "columns": [
                    {"type": "Column", "width": "auto",
                     "items": [{"type": "TextBlock", "text": "Level", "weight": "Bolder"}]},
                    {"type": "Column", "width": "stretch",
                     "items": [{"type": "TextBlock", "text": "Agent", "weight": "Bolder"}]},
                    {"type": "Column", "width": "stretch",
                     "items": [{"type": "TextBlock", "text": "Anomaly", "weight": "Bolder"}]},
                ],
            },
        ]

        for a in anomalies:
            level = a.get("alert_level", "LOW")
            agent_id = a.get("agent_id", "unknown")
            name = a.get("name", "unknown")
            rec = a.get("recommendation", "")
            card_body.append({
                "type": "ColumnSet",
                "columns": [
                    {"type": "Column", "width": "auto",
                     "items": [{"type": "TextBlock", "text": level,
                                "color": color_map.get(level, "default")}]},
                    {"type": "Column", "width": "stretch",
                     "items": [{"type": "TextBlock", "text": agent_id, "wrap": True}]},
                    {"type": "Column", "width": "stretch",
                     "items": [{"type": "TextBlock", "text": name, "wrap": True}]},
                ],
            })
            if rec:
                card_body.append({
                    "type": "TextBlock",
                    "text": f"  → {rec}",
                    "isSubtle": True,
                    "wrap": True,
                })

        fallback = self._format_alert(anomalies, state)
        return self.send_adaptive_card(card_body, fallback_text=fallback)

    def _meets_threshold(self, level: str) -> bool:
        return self._LEVEL_ORDER.get(level, 0) >= self._LEVEL_ORDER.get(self._min_level, 0)

    def _format_alert(self, anomalies: List[Dict], state: Dict[str, Any]) -> str:
        lines = [f"agent-logging-system alert — {len(anomalies)} anomaly(s) detected"]
        for a in anomalies:
            level_tag = _LEVEL_EMOJI.get(a.get("alert_level", "LOW"), "[?]")
            agent_id = a.get("agent_id", "unknown")
            name = a.get("name", "unknown")
            recommendation = a.get("recommendation", "")
            lines.append(f"  {level_tag} {agent_id}: {name}")
            if recommendation:
                lines.append(f"         -> {recommendation}")
        return "\n".join(lines)
