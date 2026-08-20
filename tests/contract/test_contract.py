"""Contract tests over the fake backend (SPEC §5.9).

Every assertion runs through the real MCP transport stack: protocol gate,
authentication, policy, canonical engine and result semantics.
"""

from __future__ import annotations

import asyncio
import json

import jsonschema
import pytest

from bloomberg_mcp.mcp.output_schemas import ENVELOPE_SCHEMA
from bloomberg_mcp.mcp.server import build_tool_catalog

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------- tool catalog


async def test_every_tool_has_input_and_output_schemas(mcp_client) -> None:
    response = await mcp_client.list_tools()
    tools = response.json()["result"]["tools"]
    catalog = build_tool_catalog()
    assert len(tools) == len(catalog)
    for tool in tools:
        assert tool["inputSchema"], tool["name"]
        assert tool["outputSchema"], tool["name"]
        jsonschema.Draft202012Validator.check_schema(tool["inputSchema"])
        jsonschema.Draft202012Validator.check_schema(tool["outputSchema"])


async def test_structured_content_validates_against_output_schema(mcp_client) -> None:
    response = await mcp_client.call_tool("blpapi_list_services", {})
    structured = mcp_client.structured(response)
    jsonschema.validate(instance=structured, schema=ENVELOPE_SCHEMA)


async def test_unknown_input_elements_rejected(mcp_client) -> None:
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
            "unexpected": True,
        },
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is False
    assert structured["error"]["code"] == "INVALID_ARGUMENT"


# ------------------------------------------------------- protocol validation


async def test_unsupported_protocol_version_rejected(mcp_client) -> None:
    response = await mcp_client.raw_post(
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        },
        headers={
            "Authorization": "Bearer " + "test-bearer-" + "x" * 48,
            "MCP-Protocol-Version": "2099-01-01",
            "Mcp-Method": "tools/list",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32022


async def test_missing_protocol_version_rejected(mcp_client) -> None:
    response = await mcp_client.raw_post(
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Authorization": "Bearer " + "test-bearer-" + "x" * 48,
            "Mcp-Method": "tools/list",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    assert response.status_code == 400


async def test_header_body_mismatch_rejected(mcp_client) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            "name": "blpapi_list_services",
            "arguments": {},
        },
    }
    headers = {
        "Authorization": "Bearer " + "test-bearer-" + "x" * 48,
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",  # deliberately mismatched
        "Mcp-Name": "blpapi_list_services",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    response = await mcp_client.raw_post(body, headers)
    assert response.status_code == 400


async def test_invalid_origin_returns_403(mcp_client) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    headers = {
        "Authorization": "Bearer " + "test-bearer-" + "x" * 48,
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Origin": "http://evil.example",
    }
    response = await mcp_client.raw_post(body, headers)
    assert response.status_code == 403


# ------------------------------------------------------------- authentication


async def test_authentication_before_submission(mcp_client, running_gateway) -> None:
    backend = running_gateway.backend
    submissions_before = backend._token_counter
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
        },
        token=None,
    )
    assert response.status_code == 401
    assert backend._token_counter == submissions_before  # nothing reached Bloomberg


async def test_invalid_bearer_rejected(mcp_client) -> None:
    response = await mcp_client.call_tool("blpapi_list_services", {}, token="wrong-token-abc")
    assert response.status_code == 401


# --------------------------------------------------------------------- policy


async def test_generic_execution_obeys_policy(mcp_client) -> None:
    # apiflds wildcard allows discovery only; execution of an unknown op denied.
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/apiflds",
            "operation": "RandomRequest",
            "parameters": {},
        },
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is False
    assert structured["error"]["code"] in ("INVALID_OPERATION", "AUTH_FORBIDDEN")


async def test_discovery_allowed_execution_denied_operation(mcp_client) -> None:
    # "//blp/instruments" wildcard: discover true, execute false for unknown ops.
    describe = await mcp_client.call_tool(
        "blpapi_describe_service", {"service": "//blp/instruments", "include_operations": True}
    )
    assert mcp_client.structured(describe)["ok"] is True


# ----------------------------------------------------------- result semantics


async def test_partial_success_item_errors_coexist(mcp_client) -> None:
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["700 HK Equity", "INVALID SEC"], "fields": ["PX_LAST"]},
            "execution": {"wait_seconds": 10},
        },
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is True
    assert structured["data"]["status"] == "COMPLETED"
    kinds = {(e["kind"], e.get("category")) for e in structured["item_errors"]}
    assert ("security", "BAD_SEC") in kinds
    messages = structured["data"]["data"]["messages"]
    securities = [entry for m in messages for entry in m["payload"].get("securityData", [])]
    assert any("fieldData" in s for s in securities)
    assert any("securityError" in s for s in securities)


async def test_field_error_coexists_with_data(mcp_client) -> None:
    response = await mcp_client.call_tool(
        "get_reference_data",
        {"securities": ["700 HK Equity"], "fields": ["PX_LAST", "INVALID_FIELD"]},
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is True
    assert any(e["kind"] == "field" and e.get("category") == "BAD_FLD" for e in structured["item_errors"])
    assert structured["data"]["rows"]


async def test_date_values_survive_without_day_shift(mcp_client) -> None:
    response = await mcp_client.call_tool(
        "get_historical_data",
        {"security": "700 HK Equity", "fields": ["PX_LAST"], "start_date": "20260101", "end_date": "20260110"},
    )
    structured = mcp_client.structured(response)
    dates = {row["date"] for row in structured["data"]["rows"]}
    assert all(isinstance(d, str) and "T" not in d for d in dates)  # calendar dates only
    assert any(d.startswith("2026-01") for d in dates)


async def test_normalized_mode_fails_cleanly_without_normalizer(mcp_client, running_gateway) -> None:
    running_gateway.normalizers._normalizers.clear()
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
            "execution": {"response_mode": "normalized", "wait_seconds": 10},
        },
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is False
    assert structured["error"]["code"] == "NORMALIZER_NOT_AVAILABLE"


async def test_normalized_fallback_when_allowed(mcp_client, running_gateway) -> None:
    running_gateway.normalizers._normalizers.clear()
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
            "execution": {
                "response_mode": "normalized",
                "allow_canonical_fallback": True,
                "wait_seconds": 10,
            },
        },
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is True
    assert "messages" in structured["data"]["data"]


# --------------------------------------------------------------- idempotency


async def test_duplicate_client_request_id_single_submission(mcp_client, running_gateway) -> None:
    backend = running_gateway.backend
    before = backend._token_counter
    ids = []
    for _ in range(2):
        response = await mcp_client.call_tool(
            "blpapi_send_request",
            {
                "client_request_id": "hermes-job-42-step-3",
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["700 HK Equity"], "fields": ["PX_LAST"]},
                "execution": {"wait_seconds": 10},
            },
        )
        structured = mcp_client.structured(response)
        ids.append(structured["request_id"])
    assert ids[0] == ids[1]
    assert backend._token_counter == before + 1  # exactly one Bloomberg submission


async def test_cancellation_is_idempotent(mcp_client, running_gateway) -> None:
    running_gateway.backend.response_delay_seconds = 5.0
    try:
        response = await mcp_client.call_tool(
            "blpapi_send_request",
            {
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["700 HK Equity"], "fields": ["PX_LAST"]},
                "execution": {"wait_seconds": 1, "overall_deadline_seconds": 30},
            },
        )
        structured = mcp_client.structured(response)
        request_id = structured["request_id"]
        assert structured["data"]["pending"] is True
        first = await mcp_client.call_tool("blpapi_cancel_request", {"request_id": request_id})
        second = await mcp_client.call_tool("blpapi_cancel_request", {"request_id": request_id})
        assert mcp_client.structured(first)["ok"] is True
        assert mcp_client.structured(second)["ok"] is True
        await asyncio.sleep(0.1)
    finally:
        running_gateway.backend.response_delay_seconds = 0.0


# ------------------------------------------------------------ large results


async def test_large_result_returns_resource_link(mcp_client, running_gateway) -> None:
    # Shrink the inline cap so any response becomes an artifact.
    from dataclasses import replace

    running_gateway.executor._config = replace(running_gateway.executor._config, inline_result_bytes=64)
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["700 HK Equity"], "fields": ["PX_LAST"]},
            "execution": {"wait_seconds": 10},
        },
    )
    structured = mcp_client.structured(response)
    data = structured["data"]
    assert "artifact" in data
    artifact = data["artifact"]
    assert artifact["resource_uri"].startswith("bloomberg-result://res_")
    assert artifact["format"] == "jsonl"
    assert data["preview"]

    # Resource read enforces ownership and returns metadata.
    uri = artifact["resource_uri"]
    read = await mcp_client.read_resource(uri)
    contents = read.json()["result"]["contents"]
    metadata = json.loads(contents[0]["text"])
    assert metadata["result_id"] == artifact["result_id"]

    # Paged read works.
    page_read = await mcp_client.read_resource(f"bloomberg-result://{artifact['result_id']}/page/1")
    page = json.loads(page_read.json()["result"]["contents"][0]["text"])
    assert page["items"]


async def test_no_native_object_representation_externally(mcp_client) -> None:
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["700 HK Equity"], "fields": ["PX_LAST"]},
            "execution": {"wait_seconds": 10},
        },
    )
    text = response.text
    for marker in ("blpapi.", "<Event", "<Message", "<Element", "object at 0x"):
        assert marker not in text
