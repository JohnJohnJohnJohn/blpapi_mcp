"""Deterministic fake Bloomberg backend (SPEC §5.8).

Implements the full canonical adapter contract without any Bloomberg
connectivity so development, CI and contract tests run anywhere. Scenarios
(error injection, session loss, schema drift, subscription failures) are
driven through explicit methods and deterministic parameter conventions;
fixtures contain no licensed production Bloomberg data.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from bloomberg_mcp.blp.backend import (
    BloombergBackend,
    ExecutionHandle,
    ServiceSummary,
    SessionListener,
    SubscriptionEvent,
    SubscriptionSink,
)
from bloomberg_mcp.blp.schema_converter import hash_operation_schema
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import (
    BloombergDatatype,
    CanonicalMessage,
    CanonicalRequest,
    ElementDescriptor,
    EventKind,
    OperationDescriptor,
    SessionState,
    utc_now,
)


def _s(name: str, **kw: Any) -> ElementDescriptor:
    # Bloomberg scalar elements carry maxValues == 1; arrays opt out below.
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=BloombergDatatype.STRING, **kw)


def _seq(name: str, children: tuple[ElementDescriptor, ...], **kw: Any) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=BloombergDatatype.SEQUENCE, children=children, **kw)


def _f64(name: str, **kw: Any) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=BloombergDatatype.FLOAT64, **kw)


def _i64(name: str, **kw: Any) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=BloombergDatatype.INT64, **kw)


def _date(name: str, **kw: Any) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=BloombergDatatype.DATE, **kw)


def _build_fake_operations(schema_variant: int) -> dict[str, dict[str, OperationDescriptor]]:
    """Build the fake service/operation schema catalog."""
    security_error = _seq(
        "securityError",
        (_s("source"), _i64("code"), _s("category"), _s("message"), _s("description")),
    )
    error_info = _seq("errorInfo", (_s("source"), _i64("code"), _s("category"), _s("message")))
    field_exception = _seq("fieldExceptions", (_s("fieldId"), error_info), max_values=None)

    reference_request = _seq(
        "ReferenceDataRequest",
        (
            _s("securities", min_values=1, max_values=None),
            _s("fields", min_values=1, max_values=None),
            _seq("overrides", (_s("fieldId"), _s("value")), max_values=None),
            ElementDescriptor(name="returnEids", datatype=BloombergDatatype.BOOL),
        ),
        type_name="ReferenceDataRequest",
    )
    reference_response = _seq(
        "ReferenceDataResponse",
        (
            _seq(
                "securityData",
                (
                    _s("security"),
                    security_error,
                    _seq("fieldData", (), type_name="ReferenceFieldData"),
                    field_exception,
                    _i64("sequenceNumber"),
                ),
                max_values=None,
            ),
        ),
        type_name="ReferenceDataResponse",
    )

    historical_request_children = [
        _s("securities", min_values=1, max_values=None),
        _s("fields", min_values=1, max_values=None),
        _s("startDate"),
        _s("endDate"),
        ElementDescriptor(
            name="periodicitySelection",
            datatype=BloombergDatatype.ENUMERATION,
            enum_values=("DAILY", "WEEKLY", "MONTHLY"),
        ),
        _seq("overrides", (_s("fieldId"), _s("value")), max_values=None),
    ]
    if schema_variant > 0:
        # Deterministic schema drift used by reconnect scenario tests.
        historical_request_children.append(_s("fakeVariantField"))
    historical_request = _seq(
        "HistoricalDataRequest", tuple(historical_request_children), type_name="HistoricalDataRequest"
    )
    historical_response = _seq(
        "HistoricalDataResponse",
        (
            _seq(
                "securityData",
                (
                    _s("security"),
                    security_error,
                    _seq(
                        "fieldData",
                        (_date("date"), _f64("value"), _s("field")),
                        max_values=None,
                        type_name="HistoricalFieldDataRow",
                    ),
                    field_exception,
                ),
                max_values=None,
            ),
        ),
        type_name="HistoricalDataResponse",
    )

    intraday_bar_request = _seq(
        "IntradayBarRequest",
        (
            _s("security", min_values=1),
            ElementDescriptor(
                name="eventType",
                datatype=BloombergDatatype.ENUMERATION,
                enum_values=("TRADE", "BID", "ASK"),
                min_values=1,
            ),
            _s("interval"),
            _s("startDateTime"),
            _s("endDateTime"),
        ),
        type_name="IntradayBarRequest",
    )
    intraday_bar_response = _seq(
        "IntradayBarResponse",
        (
            _seq(
                "barData",
                (
                    _s("security"),
                    security_error,
                    _seq(
                        "barTickData",
                        (_date("time"), _f64("open"), _f64("high"), _f64("low"), _f64("close"), _i64("volume")),
                        max_values=None,
                        type_name="BarTickData",
                    ),
                ),
            ),
        ),
        type_name="IntradayBarResponse",
    )

    intraday_tick_request = _seq(
        "IntradayTickRequest",
        (
            _s("security", min_values=1),
            _s("eventTypes", max_values=None),
            _s("startDateTime"),
            _s("endDateTime"),
        ),
        type_name="IntradayTickRequest",
    )
    intraday_tick_response = _seq(
        "IntradayTickResponse",
        (
            _seq(
                "tickData",
                (
                    _s("security"),
                    security_error,
                    _seq(
                        "tickData",
                        (_date("time"), _s("type"), _f64("value"), _i64("size")),
                        max_values=None,
                        type_name="TickData",
                    ),
                ),
            ),
        ),
        type_name="IntradayTickResponse",
    )

    instrument_request = _seq(
        "instrumentListRequest",
        (_s("query", min_values=1), _s("yellowKeyFilters", max_values=None), _s("maxResults")),
        type_name="instrumentListRequest",
    )
    instrument_response = _seq(
        "instrumentListResponse",
        (
            _seq(
                "results",
                (_s("name"), _s("yellowKey"), _seq("partialMatch", (), max_values=None)),
                max_values=None,
            ),
        ),
        type_name="instrumentListResponse",
    )

    curve_request = _seq(
        "curveListRequest",
        (_s("currency", min_values=1), _s("country"), _s("type"), _s("maxResults")),
        type_name="curveListRequest",
    )
    curve_response = _seq(
        "curveListResponse",
        (_seq("curveList", (_s("name"), _s("country"), _s("currency")), max_values=None),),
        type_name="curveListResponse",
    )

    govt_request = _seq(
        "govtListRequest",
        (_s("country", min_values=1), _s("maxResults")),
        type_name="govtListRequest",
    )
    govt_response = _seq(
        "govtListResponse",
        (_seq("govtList", (_s("name"), _s("country")), max_values=None),),
        type_name="govtListResponse",
    )

    field_search_request = _seq(
        "FieldSearchRequest",
        (_s("searchSpec", min_values=1), _s("maxResults")),
        type_name="FieldSearchRequest",
    )
    field_search_response = _seq(
        "FieldSearchResponse",
        (
            _seq(
                "fieldData",
                (_seq("fieldInfo", (_s("mnemonic"), _s("description"), _s("fieldType"))),),
                max_values=None,
            ),
        ),
        type_name="FieldSearchResponse",
    )

    def op(service: str, name: str, req: ElementDescriptor, resp: ElementDescriptor, gen: int) -> OperationDescriptor:
        descriptor = OperationDescriptor(
            service=service,
            operation=name,
            description=f"Fake {name}",
            request=req,
            responses=(resp,),
            service_generation=gen,
            schema_hash="",
        )
        return OperationDescriptor(
            service=descriptor.service,
            operation=descriptor.operation,
            description=descriptor.description,
            request=descriptor.request,
            responses=descriptor.responses,
            service_generation=descriptor.service_generation,
            schema_hash=hash_operation_schema(descriptor),
        )

    refdata = {
        "ReferenceDataRequest": op(
            "//blp/refdata", "ReferenceDataRequest", reference_request, reference_response, 1
        ),
        "HistoricalDataRequest": op(
            "//blp/refdata", "HistoricalDataRequest", historical_request, historical_response, 1
        ),
        "IntradayBarRequest": op(
            "//blp/refdata", "IntradayBarRequest", intraday_bar_request, intraday_bar_response, 1
        ),
        "IntradayTickRequest": op(
            "//blp/refdata", "IntradayTickRequest", intraday_tick_request, intraday_tick_response, 1
        ),
    }
    instruments = {
        "instrumentListRequest": op(
            "//blp/instruments", "instrumentListRequest", instrument_request, instrument_response, 1
        ),
        "curveListRequest": op(
            "//blp/instruments", "curveListRequest", curve_request, curve_response, 1
        ),
        "govtListRequest": op(
            "//blp/instruments", "govtListRequest", govt_request, govt_response, 1
        ),
    }
    apiflds = {
        "FieldSearchRequest": op("//blp/apiflds", "FieldSearchRequest", field_search_request, field_search_response, 1),
    }
    return {"//blp/refdata": refdata, "//blp/instruments": instruments, "//blp/apiflds": apiflds}


class FakeBloombergBackend(BloombergBackend):
    """In-memory backend with controllable, deterministic behavior."""

    def __init__(self, startup_services: tuple[str, ...] = ("//blp/refdata", "//blp/mktdata")) -> None:
        self._state = SessionState.STOPPED
        self._generation = 0
        self._schema_variant = 0
        self._operations = _build_fake_operations(0)
        self._opened: set[str] = set()
        self._startup_services = tuple(startup_services)
        self._token_counter = 0
        self._active_requests: dict[int, asyncio.Task[None]] = {}
        self._active_queues: dict[int, asyncio.Queue[Any]] = {}
        self._cancelled: set[int] = set()
        self._subscription_sink: SubscriptionSink | None = None
        self._session_listener: SessionListener | None = None
        self._active_subscriptions: dict[int, Mapping[str, Any]] = {}
        #: Artificial per-response delay used to exercise wait-timeout paths.
        self.response_delay_seconds: float = 0.0
        #: Count of partial responses emitted per request (default 2).
        self.partial_response_count: int = 2

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._state = SessionState.CONNECTED
        self._generation += 1
        for service in self._startup_services:
            if service in self._operations or service == "//blp/mktdata":
                self._opened.add(service)
        await self._notify_session()

    async def stop(self) -> None:
        for task in list(self._active_requests.values()):
            task.cancel()
        self._active_requests.clear()
        self._state = SessionState.STOPPED
        await self._notify_session()

    @property
    def session_state(self) -> SessionState:
        return self._state

    @property
    def session_generation(self) -> int:
        return self._generation

    def service_states(self) -> Mapping[str, bool]:
        known = {name: name in self._opened for name in self._operations}
        known["//blp/mktdata"] = "//blp/mktdata" in self._opened
        return known

    def list_service_summaries(self) -> list[ServiceSummary]:
        summaries: list[ServiceSummary] = []
        for service, operations in self._operations.items():
            hashes = sorted({o.schema_hash for o in operations.values()})
            summaries.append(
                ServiceSummary(
                    name=service,
                    opened=service in self._opened,
                    discover_allowed=True,
                    execute_allowed=True,
                    operation_count=len(operations),
                    schema_hash=hashes[0] if hashes else None,
                    session_generation=self._generation,
                )
            )
        summaries.append(
            ServiceSummary(
                name="//blp/mktdata",
                opened="//blp/mktdata" in self._opened,
                discover_allowed=True,
                execute_allowed=False,
                operation_count=0,
                schema_hash=None,
                session_generation=self._generation,
            )
        )
        return summaries

    async def open_service(self, service: str) -> None:
        await self.require_connected()
        if service not in self._operations and service != "//blp/mktdata":
            raise GatewayError(ErrorCode.BLOOMBERG_SERVICE_OPEN_FAILED, f"Unknown service {service!r}.")
        self._opened.add(service)

    def get_operation(self, service: str, operation: str) -> OperationDescriptor:
        operations = self._operations.get(service)
        if operations is None or operation not in operations:
            raise GatewayError(ErrorCode.INVALID_OPERATION, f"Unknown operation {operation!r} on {service!r}.")
        return operations[operation]

    def list_operations(self, service: str) -> list[OperationDescriptor]:
        return list(self._operations.get(service, {}).values())

    # ------------------------------------------------------------------ requests

    def _next_token(self) -> int:
        self._token_counter += 1
        return self._token_counter

    async def submit_request(self, request: CanonicalRequest, external_request_id: str) -> ExecutionHandle:
        await self.require_connected()
        if self.session_state is not SessionState.CONNECTED:
            raise GatewayError(ErrorCode.BLOOMBERG_NOT_CONNECTED, "Session not connected.", retryable=True)
        token = self._next_token()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        generation = self._generation
        task = asyncio.get_running_loop().create_task(
            self._run_request(token, generation, request, external_request_id, queue)
        )
        self._active_requests[token] = task
        self._active_queues[token] = queue
        return ExecutionHandle(native_token=token, session_generation=generation, messages=queue)

    async def _run_request(
        self,
        token: int,
        generation: int,
        request: CanonicalRequest,
        request_id: str,
        queue: asyncio.Queue[Any],
    ) -> None:
        try:
            if self.response_delay_seconds:
                await asyncio.sleep(self.response_delay_seconds)
            if token in self._cancelled:
                await queue.put(GatewayError(ErrorCode.CANCELLED, "Request cancelled."))
                return
            if self._state is not SessionState.CONNECTED:
                await queue.put(
                    GatewayError(ErrorCode.BLOOMBERG_SESSION_LOST, "Bloomberg session lost.", retryable=True)
                )
                return
            payload = self._build_final_payload(request)
            chunks = self._split_for_partials(request.operation, payload)
            total = len(chunks)
            for sequence, chunk in enumerate(chunks, start=1):
                kind = EventKind.RESPONSE if sequence == total else EventKind.PARTIAL_RESPONSE
                await queue.put(self._message(request, request_id, generation, sequence, kind, chunk))
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            await queue.put(GatewayError(ErrorCode.CANCELLED, "Request cancelled."))
        finally:
            self._active_requests.pop(token, None)
            self._active_queues.pop(token, None)

    def _split_for_partials(self, operation: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Split splittable payloads across partial + final responses.

        Mirrors Bloomberg behavior: several PARTIAL_RESPONSE messages followed
        by exactly one RESPONSE; the gateway must combine them.
        """
        parts = max(1, self.partial_response_count)
        if parts <= 1:
            return [payload]
        if operation in ("ReferenceDataRequest", "HistoricalDataRequest"):
            entries = payload.get("securityData")
            if isinstance(entries, list) and len(entries) > 1:
                size = (len(entries) + parts - 1) // parts
                return [{"securityData": entries[i : i + size]} for i in range(0, len(entries), size)]
        return [payload]

    def _message(
        self,
        request: CanonicalRequest,
        request_id: str,
        generation: int,
        sequence: int,
        kind: EventKind,
        payload: Mapping[str, Any],
    ) -> CanonicalMessage:
        response_name = f"{request.operation.replace('Request', 'Response')}"
        return CanonicalMessage(
            event_type=kind,
            message_type=response_name,
            request_id=request_id,
            service=request.service,
            session_generation=generation,
            sequence=sequence,
            received_at=utc_now().isoformat(),
            payload=payload,
        )

    def _build_final_payload(self, request: CanonicalRequest) -> dict[str, Any]:
        params = dict(request.parameters)
        operation = request.operation
        typed = request.response_mode.value == "typed"
        if operation == "ReferenceDataRequest":
            return self._reference_payload(params)
        if operation == "HistoricalDataRequest":
            return self._historical_payload(params, typed=typed)
        if operation == "IntradayBarRequest":
            return self._intraday_bar_payload(params)
        if operation == "IntradayTickRequest":
            return self._intraday_tick_payload(params)
        if operation == "instrumentListRequest":
            return {"results": [{"name": "FAKE TEST EQUITY", "yellowKey": "YK_EQTY"}]}
        if operation == "curveListRequest":
            return {"curveList": [{"name": "US Treasury", "country": "US", "currency": "USD"}]}
        if operation == "govtListRequest":
            return {"govtList": [{"name": "US T 4.25 08/15/2034", "country": "US"}]}
        if operation == "FieldSearchRequest":
            return {
                "fieldData": [
                    {"fieldInfo": {"mnemonic": "PX_LAST", "description": "Last price", "fieldType": "Price"}}
                ]
            }
        return {}

    # ------------------------------------------------------- payload builders

    @staticmethod
    def _deterministic_value(seed: str) -> float:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return round(100.0 + (digest[0] % 4000) / 10.0, 1)

    def _security_entries(self, securities: list[Any], fields: list[Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for index, security in enumerate(securities):
            sec = str(security)
            entry: dict[str, Any] = {"security": sec, "sequenceNumber": index}
            upper = sec.upper()
            if "INVALID" in upper:
                entry["securityError"] = {
                    "source": "fake@backend",
                    "code": 15,
                    "category": "BAD_SEC",
                    "message": "Invalid security, invalid ticker or parsing error",
                    "description": f"Unknown security {sec}",
                }
                entries.append(entry)
                continue
            if "NOENTITLE" in upper:
                entry["securityError"] = {
                    "source": "fake@backend",
                    "code": 10,
                    "category": "NO_AUTH",
                    "message": "No permission to retrieve requested data",
                    "description": f"Not entitled to {sec}",
                }
                entries.append(entry)
                continue
            field_data: dict[str, Any] = {}
            field_exceptions: list[dict[str, Any]] = []
            for field in fields:
                fld = str(field)
                if fld.upper() == "INVALID_FIELD":
                    field_exceptions.append(
                        {
                            "fieldId": fld,
                            "errorInfo": {
                                "source": "fake@backend",
                                "code": 6,
                                "category": "BAD_FLD",
                                "message": "Field is not valid for this request.",
                            },
                        }
                    )
                    continue
                field_data[fld] = self._deterministic_value(f"{sec}|{fld}")
            entry["fieldData"] = field_data
            if field_exceptions:
                entry["fieldExceptions"] = field_exceptions
            entries.append(entry)
        return entries

    def _reference_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        securities = params.get("securities") or []
        fields = params.get("fields") or []
        securities = securities if isinstance(securities, list) else [securities]
        fields = fields if isinstance(fields, list) else [fields]
        return {"securityData": self._security_entries(securities, fields)}

    def _historical_payload(self, params: dict[str, Any], *, typed: bool = False) -> dict[str, Any]:
        securities = params.get("securities") or []
        fields = params.get("fields") or []
        securities = securities if isinstance(securities, list) else [securities]
        fields = fields if isinstance(fields, list) else [fields]
        entries: list[dict[str, Any]] = []
        for security in securities:
            sec = str(security)
            entry: dict[str, Any] = {"security": sec}
            upper = sec.upper()
            if "INVALID" in upper:
                entry["securityError"] = {
                    "source": "fake@backend",
                    "code": 15,
                    "category": "BAD_SEC",
                    "message": "Invalid security",
                    "description": f"Unknown security {sec}",
                }
                entries.append(entry)
                continue
            if "NOENTITLE" in upper:
                entry["securityError"] = {
                    "source": "fake@backend",
                    "code": 10,
                    "category": "NO_AUTH",
                    "message": "No permission to retrieve requested data",
                    "description": f"Not entitled to {sec}",
                }
                entries.append(entry)
                continue
            rows: list[dict[str, Any]] = []
            base = int(self._deterministic_value(sec))
            start = str(params.get("startDate", "20260101"))
            try:
                day = int(start[-2:])
                month = int(start[4:6])
                year = int(start[:4])
            except ValueError:
                year, month, day = 2026, 1, 1
            for row in range(5):
                value = base + row * 0.5
                for field in fields:
                    date_text = f"{year:04d}-{month:02d}-{min(day + row, 28):02d}"
                    date_value: Any = {"$blp_type": "DATE", "value": date_text} if typed else date_text
                    rows.append(
                        {
                            "date": date_value,
                            "field": str(field),
                            "value": round(value, 2),
                        }
                    )
            entry["fieldData"] = rows
            entries.append(entry)
        return {"securityData": entries}

    def _intraday_bar_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        security = str(params.get("security", "FAKE Equity"))
        bars = []
        base = self._deterministic_value(security)
        for hour in range(9, 12):
            bars.append(
                {
                    "time": "2026-08-20",
                    "open": base,
                    "high": round(base + 2.0, 1),
                    "low": round(base - 1.5, 1),
                    "close": round(base + 0.5, 1),
                    "volume": 1000 * (hour - 8),
                }
            )
        return {"barData": {"security": security, "barTickData": bars}}

    def _intraday_tick_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        security = str(params.get("security", "FAKE Equity"))
        base = self._deterministic_value(security)
        ticks = [
            {"time": "2026-08-20", "type": "TRADE", "value": base, "size": 100},
            {"time": "2026-08-20", "type": "TRADE", "value": round(base + 0.1, 1), "size": 200},
        ]
        return {"tickData": {"security": security, "tickData": ticks}}

    async def cancel_request(self, native_token: int) -> None:
        self._cancelled.add(native_token)
        task = self._active_requests.get(native_token)
        if task is not None:
            task.cancel()

    # ------------------------------------------------------------ subscriptions

    async def subscribe(self, items: Sequence[Mapping[str, Any]], native_tokens: list[int]) -> None:
        await self.require_connected()
        for item, token in zip(items, native_tokens, strict=True):
            topic = str(item.get("topic", ""))
            self._active_subscriptions[token] = item
            if "FAIL" in topic.upper():
                await self._emit_subscription_status(
                    token, "SUBSCRIPTION_FAILURE", "SUBSCRIBING_FAILED", "Topic failed to subscribe."
                )
            else:
                await self._emit_subscription_status(token, "SUBSCRIPTION_STARTED", None, None)
                await self._emit_recap(token, item)

    async def resubscribe(self, items: Sequence[Mapping[str, Any]], native_tokens: list[int]) -> None:
        await self.subscribe(items, native_tokens)

    async def unsubscribe(self, native_tokens: list[int]) -> None:
        for token in native_tokens:
            self._active_subscriptions.pop(token, None)
            await self._emit_subscription_status(token, "UNSUBSCRIBED", None, None)

    async def _emit_subscription_status(
        self, token: int, status: str, error_code: str | None, error_message: str | None
    ) -> None:
        if self._subscription_sink is None:
            return
        await self._subscription_sink(
            SubscriptionEvent(
                native_token=token,
                kind=EventKind.SUBSCRIPTION_STATUS,
                message_type="SubscriptionStatus",
                payload={},
                received_at=utc_now().isoformat(),
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
        )

    async def _emit_recap(self, token: int, item: Mapping[str, Any]) -> None:
        fields = item.get("fields") or ()
        payload = {str(field): self._deterministic_value(f"{item.get('topic')}|{field}") for field in fields}
        await self.emit_market_data(token, payload)

    async def emit_market_data(self, token: int, payload: Mapping[str, Any]) -> None:
        """Test/soak hook: deliver one canonical market-data event."""
        if self._subscription_sink is None:
            return
        await self._subscription_sink(
            SubscriptionEvent(
                native_token=token,
                kind=EventKind.SUBSCRIPTION_DATA,
                message_type="MarketDataEvents",
                payload=payload,
                received_at=utc_now().isoformat(),
            )
        )

    async def emit_stale_event(self, token: int) -> None:
        """Deliver an event for a retired token (previous generation)."""
        await self.emit_market_data(token, {"LAST_PRICE": 0.0})

    def set_subscription_sink(self, sink: SubscriptionSink | None) -> None:
        self._subscription_sink = sink

    def set_session_listener(self, listener: SessionListener | None) -> None:
        self._session_listener = listener

    async def _notify_session(self) -> None:
        if self._session_listener is not None:
            await self._session_listener(self._state, self._generation)

    # ------------------------------------------------------- scenario controls

    async def simulate_session_loss(self) -> None:
        """Drop the session: in-flight requests fail, state degrades (SPEC §2.6)."""
        self._state = SessionState.RECONNECTING
        error = GatewayError(ErrorCode.BLOOMBERG_SESSION_LOST, "Bloomberg session lost.", retryable=True)
        for token, task in list(self._active_requests.items()):
            task.cancel()
            queue = self._active_queues.get(token)
            if queue is not None:
                await queue.put(error)
        await self._notify_session()

    async def simulate_reconnect(self, *, schema_change: bool = False) -> None:
        """Re-establish the session, bumping the generation and reopening services."""
        if schema_change:
            self._schema_variant += 1
            self._operations = _build_fake_operations(self._schema_variant)
        self._state = SessionState.CONNECTED
        self._generation += 1
        await self._notify_session()
