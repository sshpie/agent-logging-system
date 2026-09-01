"""Meraki Dashboard API adapter.

Translates Cisco Meraki Dashboard API calls into monitor observations.
Tracks rate limit headers, 429 responses, and per-endpoint performance.

Meraki enforces a global 5 calls/second rate limit per organization — a single
429 throttles ALL endpoints for that org. This adapter alarms before the limit
is exhausted:
    MEDIUM anomaly  ->  rate_limit_remaining < 20% of limit
    HIGH anomaly    ->  429 received (rate_limit_hit)

Integration paths:

    1. Manual logging (works with any HTTP client or the official SDK):

        adapter.log_api_call(
            endpoint="networks.getNetworkDevices",
            status_code=200,
            latency_ms=134.0,
            rate_limit_remaining=28,
        )

    2. Context manager (auto-times the call, handles exceptions):

        with adapter.observe("networks.getNetwork") as obs:
            result = dashboard.networks.getNetwork(networkId=net_id)
        # emits observation on __exit__; set obs.status on error

    3. 429 fast path (call explicitly on HTTP 429):

        adapter.log_rate_limit_hit("networks.getNetworkDevices", retry_after_s=1.0)

Pagination (RFC 5988): paginated calls (`Link: <url>; rel="next"`) naturally
have higher latency. Pass `is_paginated=True` to suppress latency alarms on those
operations.
"""
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from .base_adapter import BaseAdapter
from agent_logging_system.logging_agent import LoggingAgent
from agent_logging_system.observation import LATENCY_MACHINE, LATENCY_GENERATION

# Meraki rate limit floor (fraction of limit) that triggers a MEDIUM anomaly.
_RATE_WARN_FRACTION = 0.20


def _endpoint_to_agent_id(endpoint: str) -> str:
    """Normalize endpoint to agent_id: 'meraki.<endpoint>'."""
    return f"meraki.{endpoint.strip('/').replace('/', '.')}"


class _ObserveContext:
    """Mutable result bag for the observe() context manager."""

    def __init__(self):
        self.status: str = "success"
        self.status_code: int = 200
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_limit: int = 5
        self.result_count: Optional[int] = None
        self.is_paginated: bool = False
        self.error_details: Optional[Dict[str, str]] = None


class MerakiAdapter(BaseAdapter):
    """Monitor Cisco Meraki Dashboard API calls.

    Args:
        logging_agent:       The LoggingAgent instance to emit into.
        organization_id:     Meraki organization ID — used for attribution in
                             rate-limit anomaly messages.
        default_rate_limit:  Org-level calls/sec limit. Default: 5.
    """

    def __init__(
        self,
        logging_agent: LoggingAgent,
        organization_id: str = "",
        default_rate_limit: int = 5,
    ):
        super().__init__(logging_agent)
        self._org_id = organization_id
        self._default_rate_limit = default_rate_limit

    @contextmanager
    def observe(self, endpoint: str, is_paginated: bool = False) -> Iterator[_ObserveContext]:
        """Context manager that auto-times a Meraki API call.

        Example:
            with adapter.observe("networks.getNetworkDevices") as obs:
                result = dashboard.networks.getNetworkDevices(networkId=net_id)
                obs.rate_limit_remaining = int(headers["X-RateLimit-Remaining"])
                obs.result_count = len(result)

        On exception the call is logged as "failed" and the exception re-raised.
        Set obs.status = "failed" explicitly for soft failures (non-2xx).
        """
        ctx = _ObserveContext()
        ctx.is_paginated = is_paginated
        t0 = time.monotonic()
        try:
            yield ctx
            latency_ms = (time.monotonic() - t0) * 1000
            self.log_api_call(
                endpoint=endpoint,
                status_code=ctx.status_code,
                latency_ms=latency_ms,
                rate_limit_remaining=ctx.rate_limit_remaining,
                rate_limit_limit=ctx.rate_limit_limit,
                result_count=ctx.result_count,
                is_paginated=ctx.is_paginated,
                error_details=ctx.error_details,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self.log_api_call(
                endpoint=endpoint,
                status_code=ctx.status_code,
                latency_ms=latency_ms,
                rate_limit_remaining=ctx.rate_limit_remaining,
                rate_limit_limit=ctx.rate_limit_limit,
                is_paginated=ctx.is_paginated,
                error_details={"code": type(exc).__name__, "message": str(exc)},
            )
            raise

    def log_api_call(
        self,
        endpoint: str,
        status_code: int = 200,
        latency_ms: float = 0.0,
        rate_limit_remaining: Optional[int] = None,
        rate_limit_limit: Optional[int] = None,
        result_count: Optional[int] = None,
        is_paginated: bool = False,
        error_details: Optional[Dict[str, str]] = None,
    ) -> None:
        """Log a completed Meraki API call.

        Args:
            endpoint:              SDK method path or URL fragment, e.g.
                                   "networks.getNetworkDevices" or
                                   "/networks/{id}/devices".
            status_code:           HTTP status code. 429 is handled specially.
            latency_ms:            Round-trip latency.
            rate_limit_remaining:  Value from X-RateLimit-Remaining header.
                                   None if not available.
            rate_limit_limit:      Value from X-RateLimit-Limit header.
                                   Defaults to org-level default (5).
            result_count:          Number of items returned (for paginated
                                   endpoints). None if not applicable.
            is_paginated:          True if this was a paginated call following
                                   a Link: rel="next" header (RFC 5988).
                                   Suppresses latency alarms.
            error_details:         {"code": ..., "message": ...} on failure.
        """
        if status_code == 429:
            self.log_rate_limit_hit(endpoint)
            return

        agent_id = _endpoint_to_agent_id(endpoint)
        success = 200 <= status_code < 300
        status = "success" if success else "failed"
        limit = rate_limit_limit or self._default_rate_limit

        # Build output_data including rate limit context and result size.
        output_data: Dict[str, Any] = {"status_code": status_code}
        if rate_limit_remaining is not None:
            output_data["rate_limit_remaining"] = rate_limit_remaining
            output_data["rate_limit_limit"] = limit
        if result_count is not None:
            output_data["result_count"] = result_count

        # Paginated calls have naturally high latency; treat as generation.
        latency_kind = LATENCY_GENERATION if is_paginated else LATENCY_MACHINE

        self.emit_observation(
            agent_id=agent_id,
            action="api_call",
            input_data={"endpoint": endpoint},
            output_data=output_data,
            latency_ms=latency_ms,
            status=status,
            confidence=1.0 if success else 0.5,
            error_details=error_details,
            latency_kind=latency_kind,
        )

        # Warn when remaining rate limit budget is low but not yet exhausted.
        if (
            rate_limit_remaining is not None
            and limit > 0
            and (rate_limit_remaining / limit) < _RATE_WARN_FRACTION
        ):
            warn_id = f"meraki.rate_limit_budget.{self._org_id or 'org'}"
            self.emit_observation(
                agent_id=warn_id,
                action="rate_limit_warning",
                input_data={},
                output_data={
                    "remaining": rate_limit_remaining,
                    "limit": limit,
                    "endpoint": endpoint,
                },
                latency_ms=0.0,
                status="failed",
                confidence=1.0,
                error_details={
                    "code": "rate_limit_low",
                    "message": (
                        f"X-RateLimit-Remaining={rate_limit_remaining}/{limit} "
                        f"on org {self._org_id or 'unknown'}"
                    ),
                },
            )

    def log_rate_limit_hit(
        self,
        endpoint: str,
        retry_after_s: Optional[float] = None,
    ) -> None:
        """Log a Meraki 429 Too Many Requests response.

        Emitted as a failed observation on a dedicated rate_limit_hit agent_id
        so it surfaces as a HIGH anomaly immediately regardless of rolling window.

        Args:
            endpoint:      The endpoint that was throttled.
            retry_after_s: Value of the Retry-After header if present.
        """
        agent_id = f"meraki.rate_limit_hit.{self._org_id or 'org'}"
        self.emit_observation(
            agent_id=agent_id,
            action="rate_limit_hit",
            input_data={"endpoint": endpoint},
            output_data={
                "retry_after_s": retry_after_s,
                "organization_id": self._org_id,
            },
            latency_ms=0.0,
            status="failed",
            confidence=1.0,
            error_details={
                "code": "429",
                "message": (
                    f"Rate limit exceeded on org {self._org_id or 'unknown'}. "
                    f"Retry-After: {retry_after_s}s"
                    if retry_after_s
                    else f"Rate limit exceeded on org {self._org_id or 'unknown'}."
                ),
            },
        )

    def wrap_agent(self, agent):
        """Meraki adapter uses observe() and log_api_call() rather than a call_tool() proxy.

        Returns the agent unchanged. Use observe() context manager or log_api_call()
        to record calls — these give access to rate limit headers that generic
        wrapping cannot extract automatically.
        """
        return agent

    def ingest_response_headers(
        self,
        endpoint: str,
        status_code: int,
        latency_ms: float,
        headers: Dict[str, str],
        method: str = "GET",
    ) -> None:
        """Convenience: extract Meraki rate limit headers and log the call.

        Args:
            headers: Response headers dict — case-insensitive lookup attempted.
        """
        def _get(key: str) -> Optional[str]:
            return headers.get(key) or headers.get(key.lower())

        remaining_raw = _get("X-RateLimit-Remaining")
        limit_raw = _get("X-RateLimit-Limit")
        retry_raw = _get("Retry-After")

        remaining = int(remaining_raw) if remaining_raw is not None else None
        limit = int(limit_raw) if limit_raw is not None else None
        retry_after = float(retry_raw) if retry_raw is not None else None

        if status_code == 429:
            self.log_rate_limit_hit(endpoint, retry_after_s=retry_after)
        else:
            self.log_api_call(
                endpoint=endpoint,
                status_code=status_code,
                latency_ms=latency_ms,
                rate_limit_remaining=remaining,
                rate_limit_limit=limit,
            )
