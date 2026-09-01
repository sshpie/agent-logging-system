"""MCP server mode: expose the monitor as a queryable Agentic App.

Runs a minimal MCP-over-HTTP server (Streamable HTTP transport) so any
MCP-compatible AI agent — Claude Code, Claude Desktop, Cursor, VS Code,
Webex AI, ThousandEyes — can discover and query the monitor directly.

Registration: https://developer.webex.com/my-apps/new -> Create an Agentic App
Transport: Streamable HTTP (POST /mcp)
Auth: ApiKey (pass via --api-key; client sends X-Api-Key header)

Tools exposed:
    als-get-fleet-state     Full system snapshot: all agents, anomalies, recommendations
    als-get-anomalies       Active anomalies, optionally filtered by alert_level
    als-check-agent         State for a single agent_id
    als-get-recommendations All recommendations, optionally filtered by alert_level

Usage (standalone):

    from agent_logging_system import LoggingAgent
    from agent_logging_system.mcp_server import ALSMCPServer

    monitor = LoggingAgent()
    server = ALSMCPServer(monitor, host="0.0.0.0", port=8421, api_key="change-me")
    server.serve_forever()

Usage (CLI — from the repo root):

    python -m agent_logging_system.mcp_server --port 8421 --api-key change-me

"""
import argparse
import json
import threading
import http.server
from typing import Any, Dict, List, Optional

from agent_logging_system.logging_agent import LoggingAgent

_TOOLS = [
    {
        "name": "als-get-fleet-state",
        "description": (
            "Return the full agent-logging-system state: all tracked agents with "
            "their rolling-window metrics, active anomalies, and recommendations. "
            "Use this for a fleet-wide health check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "als-get-anomalies",
        "description": (
            "Return active anomalies detected by the monitor. "
            "Optionally filter by alert_level: HIGH, MEDIUM, or LOW."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "alert_level": {
                    "type": "string",
                    "enum": ["HIGH", "MEDIUM", "LOW"],
                    "description": "Filter anomalies to this level and above.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "als-check-agent",
        "description": (
            "Return the rolling-window state for a single agent, identified by "
            "agent_id. Returns null if the agent has not been seen."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent identifier, e.g. 'thousandeyes.get_alerts'.",
                }
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "als-get-recommendations",
        "description": (
            "Return named recommendations from the monitor. "
            "Optionally filter by alert_level: HIGH, MEDIUM, or LOW."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "alert_level": {
                    "type": "string",
                    "enum": ["HIGH", "MEDIUM", "LOW"],
                    "description": "Filter recommendations at or above this severity.",
                }
            },
            "required": [],
        },
    },
]

_LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _level_ge(level: str, threshold: str) -> bool:
    return _LEVEL_ORDER.get(level, 0) >= _LEVEL_ORDER.get(threshold, 0)


class _Handler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for MCP Streamable HTTP transport."""

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def _auth(self) -> bool:
        if not self.server.api_key:
            return True
        return self.headers.get("X-Api-Key") == self.server.api_key

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/mcp":
            self._respond(404, {"error": "endpoint is /mcp"})
            return

        if not self._auth():
            self._respond(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid JSON"})
            return

        msg_id = body.get("id")
        method = body.get("method", "")

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-logging-system", "version": "0.4.0"},
            }
            self._jsonrpc(msg_id, result)

        elif method == "tools/list":
            self._jsonrpc(msg_id, {"tools": _TOOLS})

        elif method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments") or {}
            result = self._dispatch(tool_name, args)
            self._jsonrpc(msg_id, result)

        elif method == "notifications/initialized":
            self._respond(204, None)

        else:
            error = {"code": -32601, "message": f"Method not found: {method}"}
            self._jsonrpc_error(msg_id, error)

    def _dispatch(self, tool_name: str, args: Dict) -> Dict:
        monitor: LoggingAgent = self.server.monitor
        state = monitor.get_system_state()

        if tool_name == "als-get-fleet-state":
            return _ok(state)

        elif tool_name == "als-get-anomalies":
            threshold = args.get("alert_level", "LOW")
            filtered = [
                a for a in state.get("anomalies", [])
                if _level_ge(a.get("alert_level", "LOW"), threshold)
            ]
            return _ok({"anomalies": filtered, "count": len(filtered)})

        elif tool_name == "als-check-agent":
            agent_id = args.get("agent_id", "")
            agent_state = state.get("agents", {}).get(agent_id)
            return _ok({"agent_id": agent_id, "state": agent_state})

        elif tool_name == "als-get-recommendations":
            threshold = args.get("alert_level", "LOW")
            recs = state.get("recommendations", [])
            # Recommendations reference anomalies — join to get level.
            anomaly_levels: Dict[str, str] = {
                a.get("agent_id", ""): a.get("alert_level", "LOW")
                for a in state.get("anomalies", [])
            }
            filtered = [
                r for r in recs
                if _level_ge(anomaly_levels.get(r.get("agent_id", ""), "LOW"), threshold)
            ]
            return _ok({"recommendations": filtered, "count": len(filtered)})

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

    def _jsonrpc(self, msg_id, result):
        payload = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        self._respond(200, payload)

    def _jsonrpc_error(self, msg_id, error):
        payload = {"jsonrpc": "2.0", "id": msg_id, "error": error}
        self._respond(200, payload)

    def _respond(self, code: int, body: Optional[Any]):
        self.send_response(code)
        if body is not None:
            encoded = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        else:
            self.end_headers()


def _ok(data: Any) -> Dict:
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
        "isError": False,
    }


class ALSMCPServer(http.server.HTTPServer):
    """MCP HTTP server wrapping a LoggingAgent.

    Args:
        monitor:  The LoggingAgent to expose. Shared — callers keep ingesting
                  observations; the server reads state on each request.
        host:     Bind address. "0.0.0.0" for all interfaces.
        port:     TCP port. 8421 by default.
        api_key:  If set, clients must send X-Api-Key: <key> header.
                  Set to "" to disable auth (local dev only).
    """

    def __init__(
        self,
        monitor: LoggingAgent,
        host: str = "127.0.0.1",
        port: int = 8421,
        api_key: str = "",
    ):
        super().__init__((host, port), _Handler)
        self.monitor = monitor
        self.api_key = api_key

    def start_background(self) -> threading.Thread:
        """Start the server in a daemon thread and return it."""
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t


def _cli():
    parser = argparse.ArgumentParser(description="agent-logging-system MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--api-key", default="", dest="api_key")
    args = parser.parse_args()

    monitor = LoggingAgent()
    server = ALSMCPServer(monitor, host=args.host, port=args.port, api_key=args.api_key)
    print(f"ALS MCP server on {args.host}:{args.port} | auth={'apikey' if args.api_key else 'none'}")
    print(f"Endpoint: http://{args.host}:{args.port}/mcp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    _cli()
