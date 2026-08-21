"""Request registry and generic execution engine (SPEC §2.8-§2.10, §3.5, §3.6).

The executor is the single path through which every Bloomberg request runs —
curated tools build canonical requests and call this engine too, so policy,
quotas, idempotency, cancellation and result handling apply uniformly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from bloomberg_mcp.blp.backend import BloombergBackend
from bloomberg_mcp.config import RequestsConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import (
    COMPLETE_REQUEST_STATUSES,
    CanonicalMessage,
    CanonicalRequest,
    EventKind,
    GatewayWarning,
    ItemError,
    RequestRecord,
    RequestStatus,
    ResponseMode,
    utc_now,
)
from bloomberg_mcp.observability.audit import AuditEvent, AuditLogger, hash_client_request_id
from bloomberg_mcp.observability.metrics import Metrics
from bloomberg_mcp.observability.usage import UsageTracker
from bloomberg_mcp.policy.quota import QuotaEngine
from bloomberg_mcp.registry.requests_registry import RequestRegistry
from bloomberg_mcp.storage.result_store import ResultStore

logger = logging.getLogger(__name__)


class RequestExecutor:
    def __init__(
        self,
        backend: BloombergBackend,
        registry: RequestRegistry,
        result_store: ResultStore,
        quota: QuotaEngine,
        usage: UsageTracker,
        audit: AuditLogger,
        metrics: Metrics,
        config: RequestsConfig,
        normalizers: Any | None = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._results = result_store
        self._quota = quota
        self._usage = usage
        self._audit = audit
        self._metrics = metrics
        self._config = config
        self._normalizers = normalizers
        self._semaphore = asyncio.Semaphore(config.max_concurrent)

    # ------------------------------------------------------------------- submit

    async def submit(
        self,
        principal_id: str,
        canonical: CanonicalRequest,
        *,
        client_request_id: str | None,
        wait_seconds: int,
        deadline_seconds: int,
        preview_items: int = 100,
        is_admin: bool = False,
        tool: str = "blpapi_send_request",
        allow_canonical_fallback: bool = False,
    ) -> dict[str, Any]:
        wait_seconds = max(1, min(wait_seconds, self._config.maximum_wait_seconds))
        deadline_seconds = max(wait_seconds, min(deadline_seconds, self._config.maximum_deadline_seconds))

        if client_request_id:
            replay = self._registry.dedupe_lookup(principal_id, client_request_id)
            if replay is not None:
                record = self._registry.get(replay, principal_id, admin=is_admin)
                if record is not None:
                    return self._handle_snapshot(record, idempotent_replay=True)

        if self._quota.entitlement_circuit_open:
            raise GatewayError(
                ErrorCode.BLOOMBERG_NOT_ENTITLED,
                "Entitlement circuit breaker is open; operator intervention required.",
            )
        self._quota.admit_request(principal_id, canonical.service, canonical.operation)
        self._usage.request_accepted(principal_id, canonical.service, canonical.operation)
        self._metrics.inc("mcp_tool_calls_total", tool=tool)

        if (
            self._registry.active_count() >= self._config.max_concurrent
            and self._registry.queued_count() >= self._config.max_queued
        ):
            raise GatewayError(ErrorCode.QUEUE_FULL, "Request queue is full; retry later.", retryable=True)

        now = utc_now()
        record = RequestRecord(
            request_id=f"req_{secrets.token_urlsafe(12)}",
            principal_id=principal_id,
            client_request_id=client_request_id,
            service=canonical.service,
            operation=canonical.operation,
            schema_hash=canonical.schema_hash,
            parameters_hash=_parameters_hash(canonical.parameters),
            created_at=now,
            deadline=now + timedelta(seconds=deadline_seconds),
            response_mode=canonical.response_mode.value,
        )
        self._registry.register(record)
        if client_request_id:
            self._registry.dedupe_register(
                principal_id, client_request_id, record.request_id, self._config.deduplication_window_seconds
            )

        done = asyncio.Event()
        outcome: dict[str, Any] = {"allow_canonical_fallback": allow_canonical_fallback}

        async def runner() -> None:
            try:
                await self._run(record, canonical, preview_items, outcome)
            except Exception:
                logger.exception("request execution crashed")
                record.status = RequestStatus.FAILED
                record.error = GatewayError(ErrorCode.INTERNAL_ERROR, "Internal gateway error.")
            finally:
                done.set()

        asyncio.get_running_loop().create_task(runner())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=wait_seconds)

        if done.is_set():
            return self._completed_result(record, outcome)
        return self._handle_snapshot(record, idempotent_replay=False)

    # ---------------------------------------------------------------- execution

    async def _run(
        self, record: RequestRecord, canonical: CanonicalRequest, preview_items: int, outcome: dict[str, Any]
    ) -> None:
        record.status = RequestStatus.QUEUED
        record.queued_at = utc_now()
        self._metrics.set_gauge("blpapi_queue_depth", self._registry.queued_count())
        async with self._semaphore:
            if record.status is RequestStatus.CANCELLING:
                record.status = RequestStatus.CANCELLED
                record.completed_at = utc_now()
                return
            try:
                handle = await self._backend.submit_request(canonical, record.request_id)
            except GatewayError as exc:
                record.status = RequestStatus.FAILED
                record.error = exc
                record.completed_at = utc_now()
                self._usage.request_failed(record.principal_id, canonical.operation)
                return
            record.status = RequestStatus.SENT
            record.sent_at = utc_now()
            record.native_token = handle.native_token
            record.session_generation = handle.session_generation
            await self._consume(record, canonical, handle.messages, preview_items, outcome)

        self._audit.record(
            AuditEvent(
                action="request",
                principal_id=record.principal_id,
                service=record.service,
                operation=record.operation,
                request_id=record.request_id,
                client_request_id_hash=(
                    hash_client_request_id(record.client_request_id) if record.client_request_id else None
                ),
                duration_ms=int((utc_now() - record.created_at).total_seconds() * 1000),
                outcome=record.status.value,
                error_code=record.error.code.value if record.error else None,
                response_bytes=record.byte_count,
                result_id=record.result_id,
            )
        )

    async def _consume(
        self,
        record: RequestRecord,
        canonical: CanonicalRequest,
        queue: asyncio.Queue[Any],
        preview_items: int,
        outcome: dict[str, Any],
    ) -> None:
        completed = False
        while not completed:
            remaining = (record.deadline - utc_now()).total_seconds()
            if remaining <= 0:
                await self._backend.cancel_request(record.native_token or 0)
                record.status = RequestStatus.TIMED_OUT
                record.error = GatewayError(ErrorCode.TIMEOUT, "Overall deadline reached.", retryable=True)
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=min(remaining, 1.0))
            except TimeoutError:
                continue
            if isinstance(item, GatewayError):
                error = item
                if record.status is RequestStatus.CANCELLING:
                    error = GatewayError(ErrorCode.CANCELLED, "Request cancelled.")
                record.status = (
                    RequestStatus.TIMED_OUT
                    if error.code is ErrorCode.TIMEOUT
                    else (RequestStatus.CANCELLED if error.code is ErrorCode.CANCELLED else RequestStatus.FAILED)
                )
                record.error = error
                if error.code is ErrorCode.BLOOMBERG_NOT_ENTITLED:
                    self._usage.entitlement_failure()
                break
            message: CanonicalMessage = item
            record.messages.append(message)
            record.event_count += 1
            if message.event_type is EventKind.PARTIAL_RESPONSE:
                record.partial_response_count += 1
                self._metrics.inc("blpapi_partial_responses_total")
                record.status = RequestStatus.PARTIAL
            payload_bytes = len(json.dumps(message.payload, separators=(",", ":")).encode("utf-8"))
            record.byte_count += payload_bytes
            self._metrics.inc("blpapi_response_bytes_total", payload_bytes)
            if record.byte_count > self._config.maximum_response_bytes:
                await self._backend.cancel_request(record.native_token or 0)
                record.status = RequestStatus.FAILED
                record.error = GatewayError(ErrorCode.RESPONSE_TOO_LARGE, "Response exceeded the size budget.")
                break
            if message.event_type is EventKind.RESPONSE:
                record.status = RequestStatus.COMPLETED
                completed = True

        record.completed_at = utc_now()
        if record.status is RequestStatus.COMPLETED:
            self._finalize(record, canonical, preview_items, outcome)
        else:
            self._usage.request_failed(record.principal_id, canonical.operation)
            self._metrics.inc("blpapi_request_failures_total", operation=canonical.operation)

    # --------------------------------------------------------------- finalizing

    def _finalize(
        self, record: RequestRecord, canonical: CanonicalRequest, preview_items: int, outcome: dict[str, Any]
    ) -> None:
        item_errors = extract_item_errors(record.messages)
        record.item_errors = item_errors
        if any(err.category == "NO_AUTH" for err in item_errors):
            self._usage.entitlement_failure()
        else:
            # A completed entitled exchange is the health probe that closes
            # the entitlement circuit breaker (SPEC §1.8).
            self._usage.entitlement_success()

        warnings: list[GatewayWarning] = list(record.warnings)
        inline_cap = self._config.inline_result_bytes
        mode = canonical.response_mode

        if mode is ResponseMode.NORMALIZED:
            normalizer = self._normalizers.get(canonical.service, canonical.operation) if self._normalizers else None
            if normalizer is None:
                if canonical.normalized_schema_version is None and not outcome.get("allow_canonical_fallback"):
                    record.status = RequestStatus.FAILED
                    record.error = GatewayError(
                        ErrorCode.NORMALIZER_NOT_AVAILABLE,
                        f"No normalizer registered for {canonical.operation!r}.",
                    )
                    return
                warnings.append(
                    GatewayWarning(
                        code="NORMALIZER_FALLBACK",
                        message="Normalized mode unavailable; returning canonical events.",
                    )
                )
                mode = ResponseMode.CANONICAL
            else:
                normalized = normalizer.normalize(record.messages, canonical)
                record.warnings = warnings
                outcome["data"] = normalized
                return

        messages_payload = [
            {
                "event_type": m.event_type.value,
                "message_type": m.message_type,
                "sequence": m.sequence,
                "received_at": m.received_at,
                "payload": dict(m.payload),
            }
            for m in record.messages
            if m.event_type in (EventKind.PARTIAL_RESPONSE, EventKind.RESPONSE)
        ]
        encoded = json.dumps(messages_payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= inline_cap:
            outcome["data"] = {"messages": messages_payload}
            return

        # Large result: persist a JSONL artifact and expose a resource link.
        lines = "\n".join(json.dumps(m, separators=(",", ":")) for m in messages_payload) + "\n"
        artifact = self._results.put(
            principal_id=record.principal_id,
            representation="typed-events" if mode is ResponseMode.TYPED else "canonical-events",
            fmt="jsonl",
            payload=lines.encode("utf-8"),
            message_count=len(messages_payload),
            ttl_seconds=self._config.result_ttl_seconds,
        )
        record.result_id = artifact.result_id
        outcome["artifact"] = artifact
        outcome["preview"] = messages_payload[: max(0, min(preview_items, 100))]
        warnings.append(
            GatewayWarning(code="LARGE_RESULT", message="Result exceeds inline limit; delivered as resource link.")
        )
        record.warnings = warnings

    # ------------------------------------------------------------------ results

    def _completed_result(self, record: RequestRecord, outcome: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": record.status.value,
            "request_id": record.request_id,
        }
        if record.status in COMPLETE_REQUEST_STATUSES and record.error is not None:
            result["error"] = record.error.to_dict()
        if "data" in outcome:
            result["data"] = outcome["data"]
        if "artifact" in outcome:
            result["artifact"] = outcome["artifact"].to_dict()
            result["preview"] = outcome.get("preview", [])
        result["item_errors"] = [_item_error_dict(e) for e in record.item_errors]
        result["warnings"] = [{"code": w.code, "message": w.message} for w in record.warnings]
        result["metadata"] = {
            "service": record.service,
            "operation": record.operation,
            "session_generation": record.session_generation,
            "response_mode": record.response_mode,
            "event_count": record.event_count,
            "partial_response_count": record.partial_response_count,
            "byte_count": record.byte_count,
            "elapsed_ms": int(((record.completed_at or utc_now()) - record.created_at).total_seconds() * 1000),
        }
        return result

    def _handle_snapshot(self, record: RequestRecord, *, idempotent_replay: bool) -> dict[str, Any]:
        return {
            "status": record.status.value,
            "request_id": record.request_id,
            "idempotent_replay": idempotent_replay,
            "pending": record.status not in COMPLETE_REQUEST_STATUSES,
            "metadata": {
                "service": record.service,
                "operation": record.operation,
                "session_generation": record.session_generation,
            },
        }

    # --------------------------------------------------------------- get/cancel

    def get_request(
        self,
        principal_id: str,
        request_id: str,
        *,
        include_preview: bool,
        limit: int,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        record = self._registry.get(request_id, principal_id, admin=is_admin)
        if record is None:
            raise GatewayError(ErrorCode.REQUEST_NOT_FOUND, "Request not found.")
        result = self._handle_snapshot(record, idempotent_replay=False)
        result["item_errors"] = [_item_error_dict(e) for e in record.item_errors]
        result["warnings"] = [{"code": w.code, "message": w.message} for w in record.warnings]
        if record.error is not None:
            result["error"] = record.error.to_dict()
        if record.result_id is not None:
            result["result_id"] = record.result_id
        if include_preview and record.status is RequestStatus.COMPLETED and not record.result_id:
            messages = [
                {"message_type": m.message_type, "sequence": m.sequence, "payload": dict(m.payload)}
                for m in record.messages
                if m.event_type in (EventKind.PARTIAL_RESPONSE, EventKind.RESPONSE)
            ][: max(1, min(limit, 1000))]
            result["preview"] = messages
        return result

    async def cancel_request(self, principal_id: str, request_id: str, *, is_admin: bool = False) -> dict[str, Any]:
        record = self._registry.get(request_id, principal_id, admin=is_admin)
        if record is None:
            raise GatewayError(ErrorCode.REQUEST_NOT_FOUND, "Request not found.")
        if record.status in COMPLETE_REQUEST_STATUSES:
            # Idempotent: cancelling a finished request reports its final state.
            return {"request_id": request_id, "status": record.status.value, "cancelled": False}
        if record.status is not RequestStatus.CANCELLING:
            record.status = RequestStatus.CANCELLING
            if record.native_token is not None:
                await self._backend.cancel_request(record.native_token)
        return {"request_id": request_id, "status": record.status.value, "cancelled": True}


# --------------------------------------------------------------------- helpers


def _parameters_hash(parameters: Mapping[str, Any]) -> str:
    import hashlib

    canonical = json.dumps(dict(parameters), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _item_error_dict(error: ItemError) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": error.kind, "code": error.code, "message": error.message}
    if error.security:
        payload["security"] = error.security
    if error.field:
        payload["field"] = error.field
    if error.category:
        payload["category"] = error.category
    return payload


def extract_item_errors(messages: list[CanonicalMessage]) -> list[ItemError]:
    """Extract per-security/per-field failures that coexist with data (SPEC §3.2)."""
    errors: list[ItemError] = []
    for message in messages:
        payload = message.payload
        containers: list[tuple[Any, str | None]] = []
        for key in ("securityData",):
            value = payload.get(key)
            if isinstance(value, list):
                containers.extend((entry, None) for entry in value if isinstance(entry, dict))
        for wrapper in ("barData", "tickData"):
            value = payload.get(wrapper)
            if isinstance(value, dict):
                containers.append((value, None))
        for entry, _ in containers:
            security = entry.get("security") if isinstance(entry.get("security"), str) else None
            security_error = entry.get("securityError")
            if isinstance(security_error, dict):
                errors.append(
                    ItemError(
                        kind="security",
                        code=str(security_error.get("code", "")),
                        message=str(security_error.get("message", ""))[:500],
                        security=security,
                        category=_as_str(security_error.get("category")),
                    )
                )
            field_exceptions = entry.get("fieldExceptions")
            if isinstance(field_exceptions, list):
                for exception in field_exceptions:
                    if not isinstance(exception, dict):
                        continue
                    info = exception.get("errorInfo") or {}
                    errors.append(
                        ItemError(
                            kind="field",
                            code=str(info.get("code", "")) if isinstance(info, dict) else "",
                            message=str(info.get("message", ""))[:500] if isinstance(info, dict) else "",
                            security=security,
                            field=_as_str(exception.get("fieldId")),
                            category=_as_str(info.get("category")) if isinstance(info, dict) else None,
                        )
                    )
    return errors


def _as_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) else None
