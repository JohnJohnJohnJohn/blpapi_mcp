"""Curated tools for common Bloomberg workflows (SPEC §3.10).

Every curated tool runs through the same canonical validation and execution
engine as generic requests — no duplicate native logic — with conservative
defaults, stable schemas, normalized output and tighter limits.
"""

from __future__ import annotations

import re
from typing import Any

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.canonical import build_canonical_request, canonical_overrides
from bloomberg_mcp.mcp.output_schemas import envelope, with_data
from bloomberg_mcp.mcp.tool_spec import ToolSpec
from bloomberg_mcp.models import ResponseMode

REFDATA = "//blp/refdata"
INSTRUMENTS = "//blp/instruments"
APIFLDS = "//blp/apiflds"

_CURATED_WAIT_SECONDS = 30
_CURATED_DEADLINE_SECONDS = 90


def _str_list(arguments: dict[str, Any], key: str, *, required: bool = True, maximum: int | None = None) -> list[str]:
    value = arguments.get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        if required:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"{key!r} must be a non-empty string or array of strings.")
        return []
    if maximum is not None and len(value) > maximum:
        raise GatewayError(ErrorCode.REQUEST_TOO_LARGE, f"{key!r} exceeds the curated limit of {maximum}.")
    return value


def _overrides(arguments: dict[str, Any]) -> list[dict[str, str]]:
    overrides = arguments.get("overrides")
    if overrides is None:
        return []
    if not isinstance(overrides, dict):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "overrides must be an object of fieldId -> value.")
    return canonical_overrides(overrides)


async def _run_normalized(
    gateway: Gateway,
    principal: Principal,
    service: str,
    operation: str,
    parameters: dict[str, Any],
    tool: str,
) -> dict[str, Any]:
    canonical = build_canonical_request(
        gateway,
        principal,
        service,
        operation,
        parameters,
        schema_hash=None,
        response_mode=ResponseMode.NORMALIZED,
    )
    result = await gateway.executor.submit(
        principal.principal_id,
        canonical,
        client_request_id=None,
        wait_seconds=_CURATED_WAIT_SECONDS,
        deadline_seconds=_CURATED_DEADLINE_SECONDS,
        is_admin=principal.admin,
        tool=tool,
    )
    if result.get("pending"):
        raise GatewayError(
            ErrorCode.TIMEOUT,
            "Curated request did not complete within its conservative wait window.",
            retryable=True,
            details={"request_id": result.get("request_id")},
        )
    failed = isinstance(result.get("error"), dict)
    return envelope(
        ok=not failed,
        request_id=result.get("request_id"),
        data=result.get("data"),
        error=result.get("error"),
        warnings=result.get("warnings", []),
        item_errors=result.get("item_errors", []),
        metadata={**result.get("metadata", {}), "tool": tool},
    )


async def get_reference_data(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "securities": _str_list(arguments, "securities", maximum=50),
        "fields": _str_list(arguments, "fields", maximum=50),
    }
    overrides = _overrides(arguments)
    if overrides:
        parameters["overrides"] = overrides
    return await _run_normalized(gateway, principal, REFDATA, "ReferenceDataRequest", parameters, "get_reference_data")


def _normalize_bbg_date(value: Any) -> str:
    """Normalize a date input to Bloomberg's canonical YYYYMMDD.

    HistoricalDataRequest declares startDate/endDate as plain strings (not
    DATE elements), and Bloomberg's historical server parses only YYYYMMDD;
    an ISO "YYYY-MM-DD" string is rejected with INVALID_START_DATE. The
    curated tool advertises both formats, so both must work.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    return text


async def get_historical_data(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    security = _str_list(arguments, "security", maximum=1)[0]
    parameters: dict[str, Any] = {
        "securities": [security],
        "fields": _str_list(arguments, "fields", maximum=25),
        "startDate": _normalize_bbg_date(arguments.get("start_date")),
        "endDate": _normalize_bbg_date(arguments.get("end_date")),
        "periodicitySelection": str(arguments.get("periodicity", "DAILY")).upper(),
    }
    overrides = _overrides(arguments)
    if overrides:
        parameters["overrides"] = overrides
    return await _run_normalized(
        gateway, principal, REFDATA, "HistoricalDataRequest", parameters, "get_historical_data"
    )


async def get_intraday_bars(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    parameters = {
        "security": _str_list(arguments, "security", maximum=1)[0],
        "eventType": str(arguments.get("event_type", "TRADE")).upper(),
        "interval": int(arguments.get("interval", 60)),
        "startDateTime": arguments.get("start_date_time"),
        "endDateTime": arguments.get("end_date_time"),
    }
    return await _run_normalized(gateway, principal, REFDATA, "IntradayBarRequest", parameters, "get_intraday_bars")


async def get_intraday_ticks(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    parameters = {
        "security": _str_list(arguments, "security", maximum=1)[0],
        "eventTypes": _str_list(arguments, "event_types", required=False) or ["TRADE"],
        "startDateTime": arguments.get("start_date_time"),
        "endDateTime": arguments.get("end_date_time"),
    }
    return await _run_normalized(gateway, principal, REFDATA, "IntradayTickRequest", parameters, "get_intraday_ticks")


_YK_FILTER_ALIASES: dict[str, str] = {
    # Friendly yellow-key names -> native YK_FILTER_* enum constants.
    "equity": "YK_FILTER_EQTY",
    "corp": "YK_FILTER_CORP",
    "corporate": "YK_FILTER_CORP",
    "bond": "YK_FILTER_CORP",
    "govt": "YK_FILTER_GOVT",
    "government": "YK_FILTER_GOVT",
    "muni": "YK_FILTER_MUNI",
    "municipal": "YK_FILTER_MUNI",
    "currency": "YK_FILTER_CURR",
    "fx": "YK_FILTER_CURR",
    "commodity": "YK_FILTER_CMDT",
    "index": "YK_FILTER_INDX",
    "mmkt": "YK_FILTER_MMKT",
    "moneymarket": "YK_FILTER_MMKT",
    "mtge": "YK_FILTER_MTGE",
    "mortgage": "YK_FILTER_MTGE",
    "prfd": "YK_FILTER_PRFD",
    "preferred": "YK_FILTER_PRFD",
    "client": "YK_FILTER_CLNT",
    "none": "YK_FILTER_NONE",
}


def _map_yellow_key_filter(value: str) -> str:
    """Map a curated friendly yellow-key value to the native YK_FILTER_* constant.

    Raw ``YK_FILTER_*`` constants pass through unchanged; unknown friendly
    names are returned verbatim so native validation reports the allowed enum.
    """
    key = value.strip().lower()
    if key in _YK_FILTER_ALIASES:
        return _YK_FILTER_ALIASES[key]
    return value


async def search_instruments(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "query": str(arguments.get("query", "")),
        "maxResults": min(int(arguments.get("max_results", 10)), 100),
    }
    yellow_keys = _str_list(arguments, "yellow_key_filters", required=False)
    if yellow_keys:
        parameters["yellowKeyFilter"] = _map_yellow_key_filter(yellow_keys[0])
    return await _run_normalized(
        gateway, principal, INSTRUMENTS, "instrumentListRequest", parameters, "search_instruments"
    )


async def search_curves(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "maxResults": min(int(arguments.get("max_results", 10)), 100),
    }
    if arguments.get("query"):
        parameters["query"] = str(arguments["query"])
    if arguments.get("country"):
        parameters["countryCode"] = str(arguments["country"])
    if arguments.get("currency"):
        parameters["currencyCode"] = str(arguments["currency"])
    if arguments.get("curve_type"):
        parameters["type"] = str(arguments["curve_type"])
    return await _run_normalized(gateway, principal, INSTRUMENTS, "curveListRequest", parameters, "search_curves")


async def search_government_securities(
    gateway: Gateway, principal: Principal, arguments: dict[str, Any]
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "query": str(arguments.get("country", "")),
        "maxResults": min(int(arguments.get("max_results", 10)), 100),
    }
    if arguments.get("ticker"):
        parameters["ticker"] = str(arguments["ticker"])
    return await _run_normalized(
        gateway, principal, INSTRUMENTS, "govtListRequest", parameters, "search_government_securities"
    )


async def search_fields(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "searchSpec": str(arguments.get("search_spec", "")),
    }
    result = await _run_normalized(gateway, principal, APIFLDS, "FieldSearchRequest", parameters, "search_fields")
    # FieldSearchRequest has no native maxResults element (verified live
    # 2026-08-21); honour the curated cap by truncating rows client-side.
    max_results = arguments.get("max_results")
    if max_results is not None:
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            data["rows"] = data["rows"][: int(max_results)]
    return result


async def get_market_snapshot(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    """Snapshot via the canonical reference engine; request templates are deferred (SPEC §3.10)."""
    fields = _str_list(arguments, "fields", required=False, maximum=25) or [
        "PX_LAST",
        "BID",
        "ASK",
        "VOLUME",
    ]
    parameters: dict[str, Any] = {
        "securities": _str_list(arguments, "securities", maximum=25),
        "fields": fields,
    }
    return await _run_normalized(
        gateway, principal, REFDATA, "ReferenceDataRequest", parameters, "get_market_snapshot"
    )


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_STRING_ARRAY = {"type": "array", "items": {"type": "string"}, "minItems": 1}
_OVERRIDES_SCHEMA = {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}

_TABLE_DATA = {
    "type": "object",
    "properties": {
        "normalized_schema_version": {"type": "string"},
        "source_service": {"type": "string"},
        "source_operation": {"type": "string"},
        "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["normalized_schema_version", "columns", "rows"],
}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="get_reference_data",
        title="Reference data",
        description=(
            "Fetch reference (static) field data for securities. Normalized tabular output. "
            "Note: overrides (e.g. CURRENCY) are forwarded to Bloomberg verbatim; "
            "currency-converted values require the Bloomberg FX-conversion entitlement — "
            "if a CURRENCY override returns unconverted values, use the generic "
            "HistoricalDataRequest with its native 'currency' element instead."
        ),
        input_schema=_schema(
            {"securities": _STRING_ARRAY, "fields": _STRING_ARRAY, "overrides": _OVERRIDES_SCHEMA},
            ["securities", "fields"],
        ),
        scope=None,
        handler=get_reference_data,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="get_historical_data",
        title="Historical data",
        description=(
            "Fetch historical field data for one security over a date range. "
            "Dates accept YYYY-MM-DD or YYYYMMDD; Bloomberg stores them as strings (YYYYMMDD). "
            "For currency-converted series use the generic HistoricalDataRequest with its "
            "native 'currency' element (the CURRENCY override requires FX entitlement)."
        ),
        input_schema=_schema(
            {
                "security": {"type": "string"},
                "fields": _STRING_ARRAY,
                "start_date": {"type": "string", "description": "YYYYMMDD or YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYYMMDD or YYYY-MM-DD"},
                "periodicity": {"type": "string", "enum": ["DAILY", "WEEKLY", "MONTHLY"], "default": "DAILY"},
                "overrides": _OVERRIDES_SCHEMA,
            },
            ["security", "fields", "start_date", "end_date"],
        ),
        scope=None,
        handler=get_historical_data,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="get_intraday_bars",
        title="Intraday bars",
        description="Fetch intraday OHLCV bars for one security.",
        input_schema=_schema(
            {
                "security": {"type": "string"},
                "event_type": {"type": "string", "enum": ["TRADE", "BID", "ASK"], "default": "TRADE"},
                "interval": {"type": "string", "enum": ["1", "5", "10", "15", "30", "60", "120"], "default": "60"},
                "start_date_time": {"type": "string"},
                "end_date_time": {"type": "string"},
            },
            ["security", "start_date_time", "end_date_time"],
        ),
        scope=None,
        handler=get_intraday_bars,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="get_intraday_ticks",
        title="Intraday ticks",
        description="Fetch intraday tick data for one security.",
        input_schema=_schema(
            {
                "security": {"type": "string"},
                "event_types": {"type": "array", "items": {"type": "string"}},
                "start_date_time": {"type": "string"},
                "end_date_time": {"type": "string"},
            },
            ["security", "start_date_time", "end_date_time"],
        ),
        scope=None,
        handler=get_intraday_ticks,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="search_instruments",
        title="Instrument search",
        description="Search Bloomberg instruments by free-text query.",
        input_schema=_schema(
            {
                "query": {"type": "string"},
                "yellow_key_filters": {"type": "array", "items": {"type": "string"}},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            },
            ["query"],
        ),
        scope=None,
        handler=search_instruments,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="search_curves",
        title="Curve search",
        description="Search yield curves by currency and optional country/type.",
        input_schema=_schema(
            {
                "currency": {"type": "string"},
                "country": {"type": "string"},
                "curve_type": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            },
            ["currency"],
        ),
        scope=None,
        handler=search_curves,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="search_government_securities",
        title="Government security search",
        description="Search government securities by country.",
        input_schema=_schema(
            {"country": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 100}},
            ["country"],
        ),
        scope=None,
        handler=search_government_securities,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="search_fields",
        title="Field search",
        description="Search Bloomberg field definitions (mnemonics) by keyword.",
        input_schema=_schema(
            {"search_spec": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 100}},
            ["search_spec"],
        ),
        scope=None,
        handler=search_fields,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
    ToolSpec(
        name="get_market_snapshot",
        title="Market snapshot",
        description="Fetch a market-data snapshot (default: last, bid, ask, volume) for securities.",
        input_schema=_schema(
            {"securities": _STRING_ARRAY, "fields": {"type": "array", "items": {"type": "string"}}},
            ["securities"],
        ),
        scope=None,
        handler=get_market_snapshot,
        output_schema=with_data(_TABLE_DATA),
        read_only=True,
    ),
]
