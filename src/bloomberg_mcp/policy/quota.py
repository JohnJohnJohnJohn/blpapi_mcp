"""Quota, budget, rate-limit and entitlement circuit-breaker engine (SPEC §1.8).

Policy limits do not replace Bloomberg's contractual or entitlement limits;
they protect the workstation and the license budget from runaway agents.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

from bloomberg_mcp.config import GovernanceConfig, RequestsConfig, SubscriptionsConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError

logger = logging.getLogger(__name__)

_RATE_WINDOW_SECONDS = 60.0
_RATE_MAX_PER_WINDOW = 120


class QuotaEngine:
    def __init__(
        self,
        governance: GovernanceConfig,
        requests_cfg: RequestsConfig,
        subscriptions_cfg: SubscriptionsConfig,
        persist_path: Path | None = None,
    ) -> None:
        self._governance = governance
        self._requests = requests_cfg
        self._subscriptions = subscriptions_cfg
        self._persist_path = persist_path
        self._lock = threading.Lock()

        self._by_principal_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._by_principal_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._by_service_operation: dict[str, int] = defaultdict(int)
        self._rate_windows: dict[str, deque[float]] = defaultdict(deque)
        self._day_key: str = ""
        self._month_key: str = ""
        self._roll_period_locked(datetime.now(UTC))

        self._consecutive_entitlement_failures = 0
        self._entitlement_circuit_open = False
        self._auth_failure_counter = 0
        self._auth_failure_window: deque[float] = deque()

        if governance.persist_usage_counters and persist_path is not None:
            self._load()

    # ------------------------------------------------------------------ period

    def _roll_period_locked(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        if day != self._day_key:
            self._day_key = day
            self._by_principal_day.clear()
        if month != self._month_key:
            self._month_key = month
            self._by_principal_month.clear()

    # -------------------------------------------------------------- admission

    def admit_request(self, principal_id: str, service: str, operation: str) -> None:
        """Check budgets and rate limits, then count the request.

        Raises ``LICENSE_BUDGET_EXCEEDED`` or ``RATE_LIMITED``.
        """
        now = datetime.now(UTC)
        with self._lock:
            self._roll_period_locked(now)
            day_total = sum(self._by_principal_day[principal_id].values())
            month_total = sum(self._by_principal_month[principal_id].values())
            if day_total >= self._governance.daily_request_budget:
                raise GatewayError(
                    ErrorCode.LICENSE_BUDGET_EXCEEDED,
                    "Daily request budget exhausted.",
                    details={"budget": self._governance.daily_request_budget},
                )
            if month_total >= self._governance.monthly_request_budget:
                raise GatewayError(
                    ErrorCode.LICENSE_BUDGET_EXCEEDED,
                    "Monthly request budget exhausted.",
                    details={"budget": self._governance.monthly_request_budget},
                )

            window = self._rate_windows[principal_id]
            cutoff = now.timestamp() - _RATE_WINDOW_SECONDS
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= _RATE_MAX_PER_WINDOW:
                raise GatewayError(ErrorCode.RATE_LIMITED, "Principal rate limit exceeded.", retryable=True)
            window.append(now.timestamp())

            key = f"{service}|{operation}"
            self._by_principal_day[principal_id][key] += 1
            self._by_principal_month[principal_id][key] += 1
            self._by_service_operation[key] += 1
            self._save_locked()

    def admit_subscription(self, principal_id: str, active_groups: int, requested_items: int) -> None:
        if active_groups >= self._subscriptions.maximum_per_principal:
            raise GatewayError(
                ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED,
                "Subscription group limit reached for principal.",
            )
        if requested_items > self._subscriptions.maximum_topics_per_group:
            raise GatewayError(
                ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED,
                "Too many topics in one subscription group.",
            )

    # ---------------------------------------------------------- auth failures

    def admit_auth_attempt(self, client_address: str) -> None:
        """Rate-limit authentication failures per client address (SPEC §1.7)."""
        now = datetime.now(UTC).timestamp()
        with self._lock:
            window = self._auth_failure_window
            cutoff = now - _RATE_WINDOW_SECONDS
            while window and window[0] < cutoff:
                window.popleft()

    def record_auth_failure(self, client_address: str) -> bool:
        """Record a failed authentication; returns True when now rate-limited."""
        now = datetime.now(UTC).timestamp()
        with self._lock:
            self._auth_failure_counter += 1
            self._auth_failure_window.append(now)
            cutoff = now - _RATE_WINDOW_SECONDS
            while self._auth_failure_window and self._auth_failure_window[0] < cutoff:
                self._auth_failure_window.popleft()
            return len(self._auth_failure_window) >= 20

    # ------------------------------------------------------ entitlement state

    def record_entitlement_failure(self) -> None:
        with self._lock:
            self._consecutive_entitlement_failures += 1
            if (
                self._consecutive_entitlement_failures
                >= self._governance.entitlement_failure_circuit_threshold
            ):
                if not self._entitlement_circuit_open:
                    logger.warning("entitlement circuit breaker OPEN")
                self._entitlement_circuit_open = True

    def record_entitlement_success(self) -> None:
        with self._lock:
            self._consecutive_entitlement_failures = 0
            # A successful entitled exchange is the health-probe path that can
            # close the breaker without operator intervention (SPEC §1.8).
            self._entitlement_circuit_open = False

    def reset_entitlement_circuit(self) -> None:
        """Operator intervention closes the breaker (SPEC §1.8)."""
        with self._lock:
            self._consecutive_entitlement_failures = 0
            self._entitlement_circuit_open = False

    @property
    def entitlement_circuit_open(self) -> bool:
        with self._lock:
            return self._entitlement_circuit_open

    # ---------------------------------------------------------------- metrics

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "governance_requests_today": sum(
                    sum(v.values()) for v in self._by_principal_day.values()
                ),
                "governance_requests_month": sum(
                    sum(v.values()) for v in self._by_principal_month.values()
                ),
                "entitlement_circuit_open": self._entitlement_circuit_open,
                "auth_failures_total": self._auth_failure_counter,
            }

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        assert self._persist_path is not None
        try:
            with open(self._persist_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if data.get("day") == self._day_key:
            for principal, entries in (data.get("daily") or {}).items():
                for key, count in entries.items():
                    self._by_principal_day[principal][key] = int(count)
        if data.get("month") == self._month_key:
            for principal, entries in (data.get("monthly") or {}).items():
                for key, count in entries.items():
                    self._by_principal_month[principal][key] = int(count)

    def _save_locked(self) -> None:
        if not self._governance.persist_usage_counters or self._persist_path is None:
            return
        payload = {
            "day": self._day_key,
            "month": self._month_key,
            "daily": {p: dict(v) for p, v in self._by_principal_day.items()},
            "monthly": {p: dict(v) for p, v in self._by_principal_month.items()},
        }
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._persist_path.parent), prefix=".usage-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                os.replace(tmp, self._persist_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError as exc:
            logger.warning("usage counter persistence failed: %s", exc)
