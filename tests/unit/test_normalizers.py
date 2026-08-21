"""Normalizer regression tests against the live decoder output shapes.

Shapes captured from the live gateway 2026-08-21: single-sequence objects
(securityData/barData/tickData as dicts, one per message), search results under
``results``, and column-wide historical fieldData rows.
"""

from __future__ import annotations

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest, EventKind, RequestCost, ResponseMode
from bloomberg_mcp.normalization.historical import HistoricalNormalizer
from bloomberg_mcp.normalization.instruments import (
    CurveSearchNormalizer,
    GovernmentSecuritySearchNormalizer,
    InstrumentSearchNormalizer,
)
from bloomberg_mcp.normalization.intraday import IntradayBarNormalizer, IntradayTickNormalizer


def _msg(payload: dict) -> CanonicalMessage:
    return CanonicalMessage(
        event_type=EventKind.RESPONSE,
        message_type="X",
        request_id="req_x",
        service="//blp/refdata",
        session_generation=1,
        sequence=0,
        received_at="2026-08-21T00:00:00+00:00",
        payload=payload,
    )


def _request(operation: str, parameters: dict) -> CanonicalRequest:
    return CanonicalRequest(
        service="//blp/refdata",
        operation=operation,
        schema_hash="sha256:test",
        parameters=parameters,
        estimated_cost=RequestCost(),
        response_mode=ResponseMode.NORMALIZED,
    )


def test_historical_expands_column_wide_rows() -> None:
    messages = [
        _msg(
            {
                "securityData": {
                    "security": "AAPL US Equity",
                    "fieldExceptions": [],
                    "fieldData": [
                        {"date": "2026-08-06", "PX_LAST": 312.41, "VOLUME": 46139901.0},
                        {"date": "2026-08-07", "PX_LAST": 313.33},
                    ],
                }
            }
        ),
        _msg(
            {
                "securityData": {
                    "security": "700 HK Equity",
                    "fieldExceptions": [],
                    "fieldData": [{"date": "2026-08-06", "PX_LAST": 479.2, "VOLUME": 21801977.0}],
                }
            }
        ),
    ]
    data = HistoricalNormalizer().normalize(messages, _request("HistoricalDataRequest", {"securities": ["x"]}))
    rows = data["rows"]
    # AAPL: 2 dates -> 3 field rows (2 + 1), 700 HK: 1 date -> 2 field rows.
    assert len(rows) == 5
    assert rows[0] == {"security": "AAPL US Equity", "date": "2026-08-06", "field": "PX_LAST", "value": 312.41}
    assert {"security": "700 HK Equity", "date": "2026-08-06", "field": "VOLUME", "value": 21801977.0} in rows
    # Calendar dates stay calendar dates (no timezone shift).
    assert {r["date"] for r in rows} == {"2026-08-06", "2026-08-07"}
    assert data["calendar_metadata"] == {"date_type": "calendar-date", "timezone": None}


def test_intraday_bar_normalizer() -> None:
    messages = [
        _msg(
            {
                "barData": {
                    "eidData": None,
                    "barTickData": [
                        {
                            "time": "2026-08-20T17:30:00",
                            "open": 311.0,
                            "high": 312.0,
                            "low": 310.5,
                            "close": 311.5,
                            "volume": 1000,
                        },
                        {
                            "time": "2026-08-20T17:35:00",
                            "open": 311.5,
                            "high": 313.0,
                            "low": 311.0,
                            "close": 312.8,
                            "volume": 800,
                        },
                    ],
                }
            }
        )
    ]
    data = IntradayBarNormalizer().normalize(
        messages, _request("IntradayBarRequest", {"security": "AAPL US Equity"})
    )
    rows = data["rows"]
    assert len(rows) == 2
    assert rows[0]["security"] == "AAPL US Equity"
    assert rows[0]["open"] == 311.0 and rows[0]["volume"] == 1000
    assert rows[1]["time"] == "2026-08-20T17:35:00"


def test_intraday_tick_normalizer() -> None:
    messages = [
        _msg(
            {
                "tickData": {
                    "eidData": None,
                    "tickData": [
                        {"time": "2026-08-20T19:00:00.045059", "type": "TRADE", "value": 311.25, "size": 100},
                        {"time": "2026-08-20T19:00:00.087", "type": "TRADE", "value": 311.26, "size": 50},
                    ],
                }
            }
        )
    ]
    data = IntradayTickNormalizer().normalize(
        messages, _request("IntradayTickRequest", {"security": "AAPL US Equity"})
    )
    rows = data["rows"]
    assert len(rows) == 2
    assert rows[0]["time"] == "2026-08-20T19:00:00.045059"
    assert rows[1]["value"] == 311.26 and rows[1]["size"] == 50


def test_instrument_search_normalizer() -> None:
    messages = [
        _msg(
            {
                "results": [
                    {"security": "AAPL US<equity>", "description": "Apple Inc (U.S.)"},
                    {"security": "MSFT US<equity>", "description": "Microsoft Corp"},
                ]
            }
        )
    ]
    data = InstrumentSearchNormalizer().normalize(
        messages, _request("instrumentListRequest", {"query": "Apple"})
    )
    assert data["rows"][0] == {
        "name": "Apple Inc (U.S.)",
        "security": "AAPL US<equity>",
        "yellow_key": "equity",
        "extra": {},
    }
    assert data["rows"][1]["yellow_key"] == "equity"


def test_curve_search_normalizer() -> None:
    messages = [
        _msg(
            {
                "results": [
                    {
                        "curve": "YCGT0025 Index",
                        "description": "US Treasury Actives Curve",
                        "country": "US",
                        "currency": "USD",
                    }
                ]
            }
        )
    ]
    data = CurveSearchNormalizer().normalize(messages, _request("curveListRequest", {"currencyCode": "USD"}))
    assert data["rows"][0] == {
        "name": "YCGT0025 Index",
        "curve": "YCGT0025 Index",
        "country": "US",
        "currency": "USD",
        "extra": {},
    }


def test_govt_search_normalizer() -> None:
    messages = [
        _msg(
            {
                "results": [
                    {"parseky": "91282CRF Govt", "name": "United States Treasury Note/Bond", "ticker": "T"}
                ]
            }
        )
    ]
    data = GovernmentSecuritySearchNormalizer().normalize(
        messages, _request("govtListRequest", {"query": "US"})
    )
    row = data["rows"][0]
    assert row["name"] == "United States Treasury Note/Bond"
    assert row["country"] is None  # not present in the decoded payload


def test_security_error_entry_skipped() -> None:
    messages = [
        _msg(
            {
                "securityData": {
                    "security": "ZZZ INVALID 1 Equity",
                    "securityError": {"source": "x", "code": 43, "category": "BAD_SEC", "message": "bad"},
                    "fieldData": [],
                }
            }
        )
    ]
    data = HistoricalNormalizer().normalize(messages, _request("HistoricalDataRequest", {"securities": ["x"]}))
    assert data["rows"] == []
