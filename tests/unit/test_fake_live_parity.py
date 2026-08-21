"""CS6 regression tests: fake-backend fidelity to live shapes (H) and
normalizer data retention (I).

Failing against 0445a48: the fake emits row-shaped historical rows, wrong
search element names, `curveList`/`govtList` payload keys, date-only intraday
times, and search normalizers drop the raw identifiers (security/curve/
parseky/ticker) and unknown fields.
"""

from __future__ import annotations

import pytest

from bloomberg_mcp.blp.fake_backend import FakeBloombergBackend
from bloomberg_mcp.blp.schema_converter import descriptor_to_json_schema
from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest, EventKind, ResponseMode
from bloomberg_mcp.normalization.fields import FieldSearchNormalizer
from bloomberg_mcp.normalization.historical import HistoricalNormalizer
from bloomberg_mcp.normalization.instruments import (
    CurveSearchNormalizer,
    GovernmentSecuritySearchNormalizer,
    InstrumentSearchNormalizer,
)


def _msg(payload: dict) -> CanonicalMessage:
    return CanonicalMessage(
        event_type=EventKind.RESPONSE,
        message_type="Response",
        request_id="req",
        service="//blp/refdata",
        session_generation=1,
        sequence=1,
        received_at="2026-08-21T00:00:00Z",
        payload=payload,
    )


def _request(operation: str, parameters: dict) -> CanonicalRequest:
    return CanonicalRequest(
        service="//blp/refdata",
        operation=operation,
        schema_hash="sha256:x",
        parameters=parameters,
        estimated_cost=None,
        response_mode=ResponseMode.NORMALIZED,
    )


@pytest.fixture()
def fake() -> FakeBloombergBackend:
    return FakeBloombergBackend()


# ---------------------------------------------------------------- H (fidelity)


def test_historical_payload_is_column_wide(fake: FakeBloombergBackend) -> None:
    payload = fake._historical_payload({"securities": ["AAPL US Equity"], "fields": ["PX_LAST", "VOLUME"]})
    entry = payload["securityData"][0]
    rows = entry["fieldData"]
    assert isinstance(rows, list) and rows
    # Column-wide: field names are dict keys, not literal "field"/"value" rows.
    assert "PX_LAST" in rows[0]
    assert "VOLUME" in rows[0]
    assert "field" not in rows[0]
    assert "value" not in rows[0]
    # And the real normalizer produces real field names.
    data = HistoricalNormalizer().normalize([_msg(payload)], _request("HistoricalDataRequest", {}))
    field_names = {row["field"] for row in data["rows"]}
    assert {"PX_LAST", "VOLUME"} <= field_names


def test_instrument_results_shape_and_retention(fake: FakeBloombergBackend) -> None:
    payload = fake._build_final_payload(
        _request("instrumentListRequest", {"query": "Apple", "maxResults": 3, "yellowKeyFilter": "YK_FILTER_EQTY"})
    )
    assert "results" in payload
    entries = payload["results"]
    assert entries and "security" in entries[0] and "description" in entries[0]
    data = InstrumentSearchNormalizer().normalize(
        [_msg(payload)], _request("instrumentListRequest", {"query": "Apple"})
    )
    row = data["rows"][0]
    # Retention: raw security string and yellow key both survive.
    assert "security" in row
    assert row["yellow_key"] is not None


def test_curve_results_shape_and_retention(fake: FakeBloombergBackend) -> None:
    payload = fake._build_final_payload(
        _request("curveListRequest", {"currencyCode": "USD", "maxResults": 3})
    )
    assert "results" in payload
    data = CurveSearchNormalizer().normalize([_msg(payload)], _request("curveListRequest", {}))
    row = data["rows"][0]
    assert "curve" in row  # the curve identifier survives
    assert row["country"] is not None
    assert row["currency"] is not None


def test_govt_results_shape_and_retention(fake: FakeBloombergBackend) -> None:
    payload = fake._build_final_payload(_request("govtListRequest", {"query": "US", "maxResults": 3}))
    assert "results" in payload
    data = GovernmentSecuritySearchNormalizer().normalize(
        [_msg(payload)], _request("govtListRequest", {"query": "US"})
    )
    row = data["rows"][0]
    assert "parseky" in row
    assert "ticker" in row
    assert row["name"] is not None


def test_field_search_payload_shape(fake: FakeBloombergBackend) -> None:
    payload = fake._build_final_payload(_request("FieldSearchRequest", {"searchSpec": "last price"}))
    assert "fieldData" in payload
    data = FieldSearchNormalizer().normalize([_msg(payload)], _request("FieldSearchRequest", {}))
    assert data["rows"]
    assert data["rows"][0]["mnemonic"] is not None


def test_intraday_time_is_naive_datetime(fake: FakeBloombergBackend) -> None:
    bars = fake._intraday_bar_payload({"security": "AAPL US Equity"})
    ticks = fake._intraday_tick_payload({"security": "AAPL US Equity"})
    for row in bars["barData"]["barTickData"]:
        assert "T" in str(row["time"])  # naive ISO datetime, not date-only
    for row in ticks["tickData"]["tickData"]:
        assert "T" in str(row["time"])


def _resolve(schema: dict) -> dict:
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return schema["$defs"][name]
    return schema


def test_search_schemas_match_live_names(fake: FakeBloombergBackend) -> None:
    inst = fake.get_operation("//blp/instruments", "instrumentListRequest")
    inst_schema = _resolve(descriptor_to_json_schema(inst.request))
    props = inst_schema["properties"]
    assert "yellowKeyFilter" in props  # singular, matching the live schema
    assert "yellowKeyFilters" not in props
    assert props["maxResults"]["type"] == "integer"

    curve = fake.get_operation("//blp/instruments", "curveListRequest")
    curve_props = _resolve(descriptor_to_json_schema(curve.request))["properties"]
    assert "countryCode" in curve_props and "currencyCode" in curve_props

    govt = fake.get_operation("//blp/instruments", "govtListRequest")
    govt_props = _resolve(descriptor_to_json_schema(govt.request))["properties"]
    assert "ticker" in govt_props and "partialMatch" in govt_props

    fields = fake.get_operation("//blp/apiflds", "FieldSearchRequest")
    field_props = _resolve(descriptor_to_json_schema(fields.request))["properties"]
    assert "maxResults" not in field_props  # live FieldSearchRequest has none


# ---------------------------------------------------------------- I (retention)


def test_unknown_fields_retained_in_extra() -> None:
    payload = {"results": [{"security": "AAPL US<equity>", "description": "Apple Inc", "id": "bbg0001"}]}
    data = InstrumentSearchNormalizer().normalize([_msg(payload)], _request("instrumentListRequest", {}))
    row = data["rows"][0]
    assert row["extra"]["id"] == "bbg0001"


def test_curve_extra_fields_retained() -> None:
    payload = {
        "results": [{"curve": "YCGT0025 Index", "description": "US Treasury Actives Curve", "country": "US",
                     "currency": "USD", "bbgid": "X123"}]
    }
    data = CurveSearchNormalizer().normalize([_msg(payload)], _request("curveListRequest", {}))
    assert data["rows"][0]["extra"]["bbgid"] == "X123"
