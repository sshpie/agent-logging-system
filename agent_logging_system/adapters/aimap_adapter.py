"""aimap adapter: feed a completed aimap JSON report into the monitor.

aimap runs three phases. Each maps to a logical agent tracked independently:

  aimap.port_discovery   phase 1  one observation per open port confirmed
  aimap.fingerprint      phase 2  one observation per service fingerprint match
  aimap.<ServiceName>    phase 3  one observation per enum_result, attributed
                                  to the enumerator that ran (Weaviate, Argilla,
                                  etc.) so per-enumerator error rates are visible

Latency: aimap does not log per-operation timing. We derive an estimated
milliseconds-per-service from the total scan_duration string and the service
count, then assign that uniform estimate to each phase-2 and phase-3 obs.
Phase-1 port observations get a fixed 1 ms (TCP SYN round-trip is sub-ms at
scanner speeds; the exact value is not alarmable here).

Usage:

    from agent_logging_system import LoggingAgent
    from agent_logging_system.adapters.aimap_adapter import AimapAdapter

    monitor = LoggingAgent()
    adapter = AimapAdapter(monitor)
    adapter.ingest_report("/tmp/aimap-report.json")

    state = monitor.get_system_state()
    print(state["anomalies"])
"""
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .base_adapter import BaseAdapter
from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.observation import LATENCY_MACHINE

# Severity -> confidence mapping for fingerprint observations.
_SEVERITY_CONFIDENCE = {
    "critical": 1.0,
    "high": 0.9,
    "medium": 0.7,
    "low": 0.5,
    "info": 0.3,
    "unknown": 0.5,
}

# auth_status values that indicate the enumerator found an exposed surface.
_UNAUTH_STATUSES = {"unauthenticated", "open", "exposed"}


def _parse_duration_ms(duration_str: str) -> Optional[float]:
    """Convert aimap scan_duration strings like '50m37s', '2m5s', '45s' to ms."""
    if not duration_str:
        return None
    pattern = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
    m = pattern.fullmatch(duration_str.strip())
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total_s = hours * 3600 + minutes * 60 + seconds
    return float(total_s * 1000) if total_s > 0 else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AimapAdapter(BaseAdapter):
    """Feed a completed aimap JSON report into LoggingAgent as structured observations."""

    PHASE1 = "aimap.port_discovery"
    PHASE2 = "aimap.fingerprint"

    def wrap_agent(self, agent: Any) -> Any:
        return agent

    def ingest_report(self, path: str) -> Dict[str, Any]:
        """Parse an aimap JSON report and emit observations for all three phases.

        Returns the full system_state snapshot after ingestion so callers can
        read anomalies and recommendations immediately.
        """
        with open(path) as f:
            report = json.load(f)

        timestamp = report.get("timestamp") or _utc_now()
        summary = report.get("summary", {})
        # aimap emits null (not []) for empty arrays — coalesce so len() works.
        services = report.get("services") or []
        enum_results = report.get("enum_results") or []

        # Derive per-service latency estimate from total scan duration.
        duration_ms = _parse_duration_ms(summary.get("scan_duration", ""))
        total_services = max(len(services), 1)
        latency_per_service = (duration_ms / total_services) if duration_ms else 1000.0

        # Phase 1: one observation per open port.
        open_ports = report.get("open_ports") or []
        for port_entry in open_ports:
            host = port_entry.get("host", "unknown") if isinstance(port_entry, dict) else str(port_entry)
            port = port_entry.get("port", 0) if isinstance(port_entry, dict) else 0
            self.emit_observation(
                agent_id=self.PHASE1,
                action="port_open",
                input_data={"host": host, "port": port},
                output_data={"open": True},
                latency_ms=1.0,
                status="success",
                timestamp_override=timestamp,
            )

        # If open_ports is empty but summary has a count, emit a single aggregate obs.
        if not open_ports and summary.get("open_ports"):
            self.emit_observation(
                agent_id=self.PHASE1,
                action="port_scan_complete",
                input_data={"targets": summary.get("total_targets", 0)},
                output_data={"open_ports": summary["open_ports"]},
                latency_ms=1.0,
                status="success",
                timestamp_override=timestamp,
            )

        # Phase 2: one observation per fingerprint match.
        for svc in services:
            service_name = svc.get("service", "unknown")
            severity = svc.get("severity", "unknown").lower()
            confidence = _SEVERITY_CONFIDENCE.get(severity, 0.5)
            self.emit_observation(
                agent_id=self.PHASE2,
                action="fingerprint_match",
                input_data={
                    "host": svc.get("host"),
                    "port": svc.get("port"),
                    "match_path": svc.get("match_path"),
                },
                output_data={
                    "service": service_name,
                    "version": svc.get("version"),
                    "severity": severity,
                },
                latency_ms=latency_per_service,
                status="success",
                confidence=confidence,
                timestamp_override=timestamp,
            )

        # Phase 3: one observation per enum_result, agent_id = enumerator name.
        for enum in enum_results:
            service_name = enum.get("service", "unknown")
            agent_id = f"aimap.{service_name}"
            auth_status = (enum.get("auth_status") or "unknown").lower()
            findings = enum.get("findings") or []
            risk_level = (enum.get("risk_level") or "info").lower()

            # Status: failed if auth unknown AND no findings AND risk is not elevated.
            # This catches the "enumerator ran but learned nothing" case.
            if auth_status == "unknown" and not findings and risk_level == "info":
                status = "failed"
                confidence = 0.3
                error_details = {"reason": "auth_unknown_no_findings"}
            elif auth_status in _UNAUTH_STATUSES:
                status = "success"
                confidence = 1.0
                error_details = None
            else:
                status = "success"
                confidence = _SEVERITY_CONFIDENCE.get(risk_level, 0.5)
                error_details = None

            self.emit_observation(
                agent_id=agent_id,
                action="enumerate",
                input_data={
                    "host": enum.get("host"),
                    "port": enum.get("port"),
                    "service": service_name,
                },
                output_data={
                    "auth_status": auth_status,
                    "risk_level": risk_level,
                    "finding_count": len(findings) if isinstance(findings, list) else 0,
                },
                latency_ms=latency_per_service,
                status=status,
                confidence=confidence,
                error_details=error_details,
                timestamp_override=timestamp,
            )

        return self.get_state()

    def emit_observation(
        self,
        agent_id: str,
        action: str,
        input_data: Any,
        output_data: Any,
        latency_ms: float,
        status: str = "success",
        confidence: float = 1.0,
        error_details=None,
        latency_kind: str = LATENCY_MACHINE,
        timestamp_override: Optional[str] = None,
    ) -> None:
        """Override to support timestamp_override for replaying historical reports."""
        from agent_logging_system.observation import Observation
        obs = Observation(
            timestamp=timestamp_override or _utc_now(),
            agent_id=agent_id,
            action=action,
            input=input_data,
            output=output_data,
            latency_ms=latency_ms,
            status=status,
            confidence=confidence,
            error_details=error_details,
            latency_kind=latency_kind,
        )
        self.logging_agent.ingest(obs)
