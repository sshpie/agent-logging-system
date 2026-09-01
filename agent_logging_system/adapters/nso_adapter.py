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

    4. Log file ingest (replaces one-shot grep triage scripts):

        # Scan last 300 lines of each NSO log file (matches tail -n 300 behavior).
        for name in ["ncs.log", "devel.log", "audit.log"]:
            adapter.ingest_log_file(f"/var/log/ncs/{name}", tail_lines=300)

        # Or parse text directly (e.g. from ncs --printlog output):
        adapter.ingest_log_lines(log_text, label="ncserr.log")

        state = monitor.get_system_state()
        # Repeated failures across scans surface as rolling-window anomalies.

    5. Live follow mode (replaces `tail -F | grep`):

        # Blocks; yields a state snapshot each time a new error line appears.
        for state in adapter.follow_log_file("/var/log/ncs/ncs.log"):
            notifier.notify_if_anomalies_card(state)

NSO RESTCONF base URL:   https://<host>:8080/restconf
NSO PyAPI:               import ncs; with ncs.maapi.Maapi() as m: ...
"""
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base_adapter import BaseAdapter
from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.observation import LATENCY_MACHINE, LATENCY_GENERATION

# Severity pre-filter — mirrors PATTERN in agent-logging-cisco.sh.
# A line must match this before operation classification runs.
# This prevents INFO/DEBUG lines with operation keywords from becoming failures.
_SEVERITY_RE = re.compile(
    r'\b(error|err|fail|crit|warn|warning|exception|abort|panic|timeout|denied|reject)\b',
    re.I,
)

# Operation classification patterns (applied only to lines that pass _SEVERITY_RE).
# Tuple: (compiled regex, canonical operation, error code label, alert weight)
# Weight mirrors the shell script scoring: internal=3, callback/sync=2, others=1.
_LOG_SIGNALS: List[Tuple[re.Pattern, str, str, int]] = [
    (re.compile(r'CDB boot error|upgrade failed', re.I), "_internal",   "cdb_or_upgrade",   3),
    (re.compile(r'internal error|panic',          re.I), "_internal",   "internal_error",   3),
    (re.compile(r'\babort\b',                     re.I), "_internal",   "abort",            3),
    (re.compile(r'transaction failure',           re.I), "commit",      "transaction_fail", 2),
    (re.compile(r'out of sync',                   re.I), "check_sync",  "out_of_sync",      2),
    (re.compile(r'sync.from',                     re.I), "sync_from",   "sync_from_error",  2),
    (re.compile(r'\bcallback\b',                  re.I), "_callbacks",  "callback_error",   2),
    (re.compile(r'bad config',                    re.I), "_config",     "bad_config",       1),
    (re.compile(r'\btemplate\b',                  re.I), "_template",   "template_error",   1),
    (re.compile(r'\bxpath\b',                     re.I), "_xpath",      "xpath_error",      1),
    (re.compile(r'\btimeout\b',                   re.I), "_timeout",    "timeout",          1),
    (re.compile(r'\bdenied\b|\breject\b',         re.I), "_access",     "access_denied",    1),
]

# Device name patterns in NSO log lines.
_DEVICE_RE = re.compile(r'\bdevice[= ]+([a-zA-Z0-9_\-\.]+)', re.I)

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

    def ingest_log_lines(
        self,
        text: str,
        label: str = "",
    ) -> Dict[str, Any]:
        """Parse NSO log text and convert signal lines into observations.

        Replaces the pattern-matching in agent-logging-cisco.sh with structured
        observations that feed into the rolling window and anomaly detector.
        Each matched line becomes one failed observation. Unmatched lines are
        ignored — only lines carrying a known failure signal are recorded.

        Signal mapping (mirrors the shell script scoring):
            internal error / panic / abort / CDB boot / upgrade failed  → HIGH weight (3)
            transaction failure / out of sync / sync-from error         → MEDIUM weight (2)
            callback error                                               → MEDIUM weight (2)
            bad config / template / xpath / timeout / denied            → LOW weight (1)

        Device names are extracted from log lines where present
        (e.g. "sync-from failed for device edge-router-01") so failures are
        tracked per device, not just per operation class.

        Args:
            text:  Raw log content — ncs.log, devel.log, audit.log, ncs-java-vm.log,
                   ncs-python-vm.log, or ncserr.log output.
            label: Log file label for agent_id namespacing (e.g. "ncs.log").
                   Observations are tagged as "nso.<op>[.<device>]".

        Returns the system state snapshot after ingestion.
        """
        matched = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue

            # Severity pre-filter: skip INFO/DEBUG lines even if they mention
            # operation keywords. Mirrors `grep -inE "$PATTERN"` in the shell script.
            if not _SEVERITY_RE.search(line):
                continue

            for pattern, op, code, _weight in _LOG_SIGNALS:
                if not pattern.search(line):
                    continue

                # Try to extract a device name from the line.
                device_match = _DEVICE_RE.search(line)
                device = device_match.group(1) if device_match else ""

                self.log_operation(
                    operation=op,
                    device=device,
                    status="failed",
                    latency_ms=0.0,
                    error_details={
                        "code": code,
                        "message": line[:200],   # cap line length in error_details
                        "source": label or "log",
                    },
                )
                matched += 1
                break  # one observation per line; first matching signal wins

        return self.get_state()

    def ingest_log_file(
        self,
        path: str,
        label: str = "",
        tail_lines: int = 0,
    ) -> Dict[str, Any]:
        """Read an NSO log file and pass it through ingest_log_lines().

        Skips silently if the file does not exist (mirrors the shell script's
        `[ -f "$file" ] || return 0` guard).

        Args:
            path:       Absolute path to the log file, e.g. "/var/log/ncs/ncs.log".
            label:      Passed to ingest_log_lines() for agent_id namespacing.
                        Defaults to the filename component of path.
            tail_lines: If > 0, only parse the last N lines of the file —
                        matches `tail -n N` behavior in the triage scripts.
                        0 (default) reads the full file.

        Returns the system state snapshot after ingestion.
        """
        import os
        if not os.path.isfile(path):
            return self.get_state()

        file_label = label or os.path.basename(path)
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().splitlines()

        if tail_lines > 0:
            lines = lines[-tail_lines:]

        return self.ingest_log_lines("\n".join(lines), label=file_label)

    def follow_log_file(
        self,
        path: str,
        label: str = "",
        poll_interval: float = 1.0,
    ):
        """Generator: watch an NSO log file for new error lines in real-time.

        Mirrors `tail -n 0 -F <file> | grep -inE "$PATTERN"` from the follow
        mode in the triage script. Seeks to the end of the file on startup so
        only new lines (written after this call) are processed.

        Yields a system state snapshot each time a new matching line appears.
        Does not yield on quiet intervals — only on new signal.

        Usage:

            adapter = NSOAdapter(monitor)
            for state in adapter.follow_log_file("/var/log/ncs/ncs.log"):
                notifier.notify_if_anomalies_card(state)

        Args:
            path:          Path to the NSO log file to watch.
            label:         Passed through to ingest_log_lines() for namespacing.
            poll_interval: Seconds to sleep between read attempts when no new
                           data is available. Default: 1.0.

        Raises:
            FileNotFoundError: if the file does not exist at call time.
        """
        import os
        file_label = label or os.path.basename(path)

        with open(path, "r", errors="replace") as fh:
            fh.seek(0, 2)   # jump to end — only tail new content

            while True:
                line = fh.readline()
                if not line:
                    time.sleep(poll_interval)
                    continue

                line = line.rstrip()
                if not line:
                    continue

                # Only emit when the line carries a severity signal.
                if not _SEVERITY_RE.search(line):
                    continue

                # Classify and emit — reuse ingest_log_lines on one line at a time.
                state = self.ingest_log_lines(line, label=file_label)
                yield state
