"""Request record store and idempotency index (SPEC §2.9, §2.10)."""

from __future__ import annotations

import threading
from datetime import timedelta

from bloomberg_mcp.models import COMPLETE_REQUEST_STATUSES, RequestRecord, utc_now

MAX_RECORDS = 10_000

# Statuses that count against the in-flight admission bound: pre-execution
# (RECEIVED), queued (QUEUED) and actively running (SENT/PARTIAL/CANCELLING).
_IN_FLIGHT_STATUSES = {"RECEIVED", "QUEUED", "SENT", "PARTIAL", "CANCELLING"}


class RequestRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, RequestRecord] = {}
        # (principal_id, client_request_id) -> (request_id, expires_at)
        self._dedup: dict[tuple[str, str], tuple[str, object]] = {}

    def register(self, record: RequestRecord) -> None:
        with self._lock:
            self._records[record.request_id] = record
            if len(self._records) > MAX_RECORDS:
                self._evict_locked()

    def try_register(self, record: RequestRecord, max_total: int) -> bool:
        """Atomically admit a new request iff in-flight headroom remains.

        The count and the insertion happen under the same lock, so two
        concurrent submits cannot both pass the bound (finding D).
        """
        with self._lock:
            in_flight = sum(1 for rec in self._records.values() if rec.status.value in _IN_FLIGHT_STATUSES)
            if in_flight >= max_total:
                return False
            self._records[record.request_id] = record
            if len(self._records) > MAX_RECORDS:
                self._evict_locked()
            return True

    def unregister(self, request_id: str) -> None:
        """Remove a record that never ran (e.g. quota-denied after admission)."""
        with self._lock:
            self._records.pop(request_id, None)

    def _evict_locked(self) -> None:
        overflow = len(self._records) - MAX_RECORDS
        if overflow <= 0:
            return
        # Prefer evicting the oldest non-in-flight records (completed first,
        # then pre-execution RECEIVED/QUEUED); never evict SENT/PARTIAL/CANCELLING.
        never_evict = {"SENT", "PARTIAL", "CANCELLING"}
        candidates = sorted(
            (
                (rid, rec)
                for rid, rec in self._records.items()
                if rec.status.value not in never_evict
            ),
            key=lambda pair: pair[1].created_at,
        )
        for rid, _ in candidates[:overflow]:
            self._records.pop(rid, None)

    def get(self, request_id: str, principal_id: str, *, admin: bool = False) -> RequestRecord | None:
        with self._lock:
            record = self._records.get(request_id)
        if record is None:
            return None
        if record.principal_id != principal_id and not admin:
            # Cross-principal access is denied with the same answer as absence.
            return None
        return record

    def all_records(self) -> list[RequestRecord]:
        with self._lock:
            return list(self._records.values())

    def active_count(self) -> int:
        active = {"SENT", "PARTIAL", "CANCELLING"}
        with self._lock:
            return sum(1 for rec in self._records.values() if rec.status.value in active)

    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for rec in self._records.values() if rec.status.value == "QUEUED")

    # ------------------------------------------------------------- idempotency

    def dedupe_register(self, principal_id: str, client_request_id: str, request_id: str, window_seconds: int) -> None:
        expires = utc_now() + timedelta(seconds=window_seconds)
        with self._lock:
            self._dedup[(principal_id, client_request_id)] = (request_id, expires)

    def dedupe_lookup(self, principal_id: str, client_request_id: str) -> str | None:
        now = utc_now()
        key = (principal_id, client_request_id)
        with self._lock:
            entry = self._dedup.get(key)
            if entry is None:
                return None
            request_id, expires = entry
            if now >= expires:  # type: ignore[operator]
                self._dedup.pop(key, None)
                return None
            return request_id

    def sweep(self, record_ttl_seconds: int | None = None) -> int:
        now = utc_now()
        removed = 0
        with self._lock:
            for key in [k for k, (_, expires) in self._dedup.items() if now >= expires]:  # type: ignore[operator]
                self._dedup.pop(key, None)
                removed += 1
            if record_ttl_seconds is not None and record_ttl_seconds > 0:
                for rid, rec in list(self._records.items()):
                    if (
                        rec.status.value in COMPLETE_REQUEST_STATUSES
                        and rec.expires_at is not None
                        and now >= rec.expires_at
                    ):
                        self._records.pop(rid, None)
                        removed += 1
        return removed
