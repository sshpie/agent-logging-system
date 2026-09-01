"""NSO (Cisco Crosswork Network Services Orchestrator) adapter.

Translates NSO RESTCONF and NETCONF operation outcomes into monitor observations.

NSO is a network lifecycle orchestrator managing multi-vendor device configuration.
Its operations have widely varying expected latency:

    Fast (< 500ms expected):   check-sync, get-config, show-interfaces
    Medium (< 5s expected):    commit, commit dry-run, rollback
    Slow (< 30s expected):     sync-from, sync-to, deploy-service, re-deploy
    Variable (bulk):           sync-all (entire device tree) — treated as generation

Operations that cross from expected into anomalous ranges indicate:
    - Device unreachability       (sync-from timeout)
    - Service logic errors        (deploy-service failures)
    - Commit conflicts            (commit failures in concurrent environments)
    - NETCONF session exhaustion  (connection errors under load)

Integration paths:

    1. Manual log (works with any client: NSO PyAPI, RESTCONF urllib, ncs CLI):

        adapter.log_operation(
            operation="sync_from",
            device="edge-router-01",
            status="success",
            latency_ms=4200.0,
        )

    2. Context manager (auto-times, handles exceptions):

        with adapter.observe("commit_dry_run") as obs:
            result = nso.maapi.commit_params()

    3. RESTCONF response ingest (after any urllib/requests call):

        adapter.ingest_restconf_response(
            path="/restconf/operations/tailf-ncs:sync-from",
            method="POST",
            status_code=200,
            latency_ms=6100.0,
            body={"sync-from-result": [{"device": "edge-01", "result": True}]},
        )

NSO RESTCONF base URL:   https://<host>:8080/restconf
NSO PyAPI:               import ncs; with ncs.maapi.Maapi() as m: ...
"""
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from .base_adapter import BaseAdapter
from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.observation import LATENCY_MACHINE, LATENCY_GENERATION

# Operations treated as long-running/generation — no latency alarms.
_GENERATION_OPS = {
    "sync_all",
    "sync-all",
    "re_deploy_all",
    "re-deploy-all",
    "check_sync_all",
    "check-sync-all",
}

# RESTCONF path fragment → canonical operation name.
_PATH_TO_OP = {
    "sync-from":          "sync_from",
    "sync-to":            "sync_to",
    "check-sync":         "check_sync",
    "re-deploy":          "re_deploy",
    "commit":             "commit",
    "rollback":           "rollback",
    "get-schema":         "get_schema",
    "get-config":         "get_config",
    "edit-config":        "edit_config",
    "tailf-ncs:sync-from":    "sync_from",
    "tailf-ncs:check-sync":   "check_sync",
    "tailf-ncs:re-deploy":    "re_deploy",
}


def _canonical_op(op: str) -> str:
    return op.replace("-", "_").lower()


class _ObserveContext:
    def __init__(self):
        self.status: str = "success"
        self.device: str = ""
        self.service: str = ""
        self.result: Optional[Any] = None
        self.error_details: Optional[Dict[str, str]] = None


class NSOAdapter(BaseAdapter):
    """Monitor Cisco NSO RESTCONF / NETCONF / PyAPI operations.

    Args:
        logging_agent:  The LoggingAgent instance to emit into.
        nso_host:       NSO server hostname or IP — used for attribution only.
    """

    def __init__(self, logging_agent: LoggingAgent, nso_host: str = ""):
        super().__init__(logging_agent)
        self._host = nso_host

    def wrap_agent(self, agent):
        """NSO adapter uses observe() and log_operation() rather than a call_tool() proxy.

        Returns the agent unchanged. Use observe() context manager or log_operation()
        to record operations — NSO's PyAPI and RESTCONF interfaces don't follow
        a uniform call_tool() pattern that generic wrapping can intercept.
        """
        return agent

    @contextmanager
    def observe(
        self,
        operation: str,
        device: str = "",
        service: str = "",
    ) -> Iterator[_ObserveContext]:
        """Context manager that times an NSO operation.

        Example:
            with adapter.observe("sync_from", device="edge-router-01") as obs:
                result = m.sync_from("edge-router-01")
                obs.result = result

        On exception: logged as "failed" and exception re-raised.
        """
        ctx = _ObserveContext()
        ctx.device = device
        ctx.service = service
        t0 = time.monotonic()
        try:
            yield ctx
            latency_ms = (time.monotonic() - t0) * 1000
            self.log_operation(
                operation=operation,
                device=ctx.device,
                service=ctx.service,
                status=ctx.status,
                latency_ms=latency_ms,
                output_data=ctx.result,
                error_details=ctx.error_details,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self.log_operation(
                operation=operation,
                device=ctx.device,
                service=ctx.service,
                status="failed",
                latency_ms=latency_ms,
                error_details={"code": type(exc).__name__, "message": str(exc)},
            )
            raise

    def log_operation(
        self,
        operation: str,
        device: str = "",
        service: str = "",
        status: str = "success",
        latency_ms: float = 0.0,
        output_data: Any = None,
        error_details: Optional[Dict[str, str]] = None,
    ) -> None:
        """Log one NSO operation outcome.

        Args:
            operation:     NSO operation name. Use snake_case constants:
                           "sync_from", "sync_to", "check_sync", "commit",
                           "commit_dry_run", "rollback", "re_deploy",
                           "deploy_service", "get_config", "edit_config".
            device:        Target device name (for device-scoped operations).
            service:       Service instance path (for service operations).
            status:        "success" | "failed" | "partial" | "in_progress".
            latency_ms:    Operation duration.
            output_data:   Operation result or summary.
            error_details: {"code": ..., "message": ...} on failure.
        """
        op = _canonical_op(operation)

        # agent_id scoping: device ops are per-device, service ops per-service.
        if device:
            agent_id = f"nso.{op}.{device}"
        elif service:
            agent_id = f"nso.{op}.service"
        else:
            agent_id = f"nso.{op}"

        mapped_status = "failed" if status not in ("success", "partial") else status
        if status == "partial":
            mapped_status = "success"  # partial sync is still progress

        latency_kind = LATENCY_GENERATION if op in _GENERATION_OPS else LATENCY_MACHINE

        input_data: Dict[str, Any] = {"operation": op}
        if device:
            input_data["device"] = device
        if service:
            input_data["service"] = service

        self.emit_observation(
            agent_id=agent_id,
            action=op,
            input_data=input_data,
            output_data=output_data,
            latency_ms=latency_ms,
            status=mapped_status,
            confidence=1.0 if mapped_status == "success" else 0.5,
            error_details=error_details,
            latency_kind=latency_kind,
        )

    def ingest_restconf_response(
        self,
        path: str,
        method: str = "GET",
        status_code: int = 200,
        latency_ms: float = 0.0,
        body: Optional[Any] = None,
    ) -> None:
        """Translate an NSO RESTCONF HTTP response into an observation.

        Parses the RESTCONF path to extract operation and device context.

        Example:
            POST /restconf/operations/tailf-ncs:sync-from
            Response body: {"sync-from-result": [{"device": "r01", "result": true}]}

        Args:
            path:        RESTCONF URL path (full or relative to /restconf).
            method:      HTTP method.
            status_code: HTTP response status.
            latency_ms:  Round-trip latency.
            body:        Parsed response body (dict/list).
        """
        # Extract operation name from path.
        op = "unknown"
        device = ""
        for fragment, canonical in _PATH_TO_OP.items():
            if fragment in path:
                op = canonical
                break

        # Try to pull device name from path: /restconf/data/tailf-ncs:devices/device=<name>/...
        if "device=" in path:
            try:
                device = path.split("device=")[1].split("/")[0]
            except IndexError:
                pass

        success = 200 <= status_code < 300
        status = "success" if success else "failed"

        error_details = None
        if not success:
            error_details = {
                "code": str(status_code),
                "message": f"RESTCONF {method} {path} returned {status_code}",
            }

        self.log_operation(
            operation=op,
            device=device,
            status=status,
            latency_ms=latency_ms,
            output_data=body,
            error_details=error_details,
        )

    def log_commit(
        self,
        dry_run: bool = False,
        status: str = "success",
        latency_ms: float = 0.0,
        changeset_size: Optional[int] = None,
        error_details: Optional[Dict[str, str]] = None,
    ) -> None:
        """Log an NSO commit or commit dry-run.

        A dry-run should complete in < 200ms for small changesets.
        A real commit can range from 100ms to several seconds depending on
        device count and protocol (NETCONF vs CLI).
        """
        op = "commit_dry_run" if dry_run else "commit"
        output: Dict[str, Any] = {}
        if changeset_size is not None:
            output["changeset_size"] = changeset_size

        self.log_operation(
            operation=op,
            status=status,
            latency_ms=latency_ms,
            output_data=output or None,
            error_details=error_details,
        )
