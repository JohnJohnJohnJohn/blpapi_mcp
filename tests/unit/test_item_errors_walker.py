"""CS5 regression tests: shared canonical walkers + item-error extraction (G).

Failing against 0445a48: `extract_item_errors` only handled LIST-shaped
`securityData`; the canonical decoder emits single-entry sequences as dicts,
so single-security requests silently lost their item errors. Fixtures mirror
the live shapes captured during acceptance testing.
"""

from __future__ import annotations

from bloomberg_mcp.canonical.walk import iter_sequence, walk_sequence
from bloomberg_mcp.models import CanonicalMessage, EventKind
from bloomberg_mcp.registry.requests import extract_item_errors


def _msg(payload: dict) -> CanonicalMessage:
    return CanonicalMessage(
        event_type=EventKind.RESPONSE,
        message_type="ReferenceDataResponse",
        request_id=None,
        service=None,
        session_generation=1,
        sequence=0,
        received_at="2026-08-21T00:00:00Z",
        payload=payload,
    )


def test_security_data_dict_single_security() -> None:
    """Live shape: one security => securityData decodes as a dict, not a list."""
    payload = {
        "securityData": {
            "security": "NOT_A_REAL_SECURITY XX",
            "securityError": {
                "source": "7::9",
                "code": 43,
                "category": "BAD_SEC",
                "message": "Unknown/Invalid Security  [nid:23107]",
                "subcategory": "INVALID_SECURITY",
            },
        }
    }
    errors = extract_item_errors([_msg(payload)])
    assert len(errors) == 1
    assert errors[0].kind == "security"
    assert errors[0].code == "43"
    assert errors[0].category == "BAD_SEC"
    assert errors[0].security == "NOT_A_REAL_SECURITY XX"


def test_security_data_list_multiple() -> None:
    """Live shape: multiple securities => securityData decodes as a list."""
    payload = {
        "securityData": [
            {
                "security": "AAPL US Equity",
                "fieldData": {"PX_LAST": 311.3},
            },
            {
                "security": "BAD SEC",
                "securityError": {"code": 43, "category": "BAD_SEC", "message": "bad"},
            },
        ]
    }
    errors = extract_item_errors([_msg(payload)])
    assert len(errors) == 1
    assert errors[0].security == "BAD SEC"


def test_field_exceptions_single_dict() -> None:
    """A single fieldException can also decode as a dict."""
    payload = {
        "securityData": {
            "security": "AAPL US Equity",
            "fieldExceptions": {
                "fieldId": "NOT_A_REAL_FIELD",
                "errorInfo": {"code": 9, "category": "BAD_FLD", "message": "Field not valid"},
            },
        }
    }
    errors = extract_item_errors([_msg(payload)])
    assert len(errors) == 1
    assert errors[0].kind == "field"
    assert errors[0].field == "NOT_A_REAL_FIELD"
    assert errors[0].code == "9"
    assert errors[0].category == "BAD_FLD"


def test_mixed_valid_and_invalid_securities() -> None:
    """Partial success: valid data rows coexist with item errors."""
    payload = {
        "securityData": [
            {
                "security": "AAPL US Equity",
                "fieldData": {"PX_LAST": 311.3, "CRNCY": "USD"},
            },
            {
                "security": "NOT_A_REAL_SECURITY XX",
                "securityError": {"code": 43, "category": "BAD_SEC", "message": "Unknown/Invalid Security"},
            },
            {
                "security": "700 HK Equity",
                "fieldExceptions": {
                    "fieldId": "PX_LAST",
                    "errorInfo": {"code": 9, "category": "BAD_FLD", "message": "Field not valid"},
                },
            },
        ]
    }
    errors = extract_item_errors([_msg(payload)])
    kinds = sorted(e.kind for e in errors)
    assert kinds == ["field", "security"]


def test_partial_response_messages_each_walked() -> None:
    """Errors across PARTIAL and final RESPONSE messages are all surfaced."""
    partial = CanonicalMessage(
        event_type=EventKind.PARTIAL_RESPONSE,
        message_type="ReferenceDataResponse",
        request_id=None,
        service=None,
        session_generation=1,
        sequence=0,
        received_at="2026-08-21T00:00:00Z",
        payload={
            "securityData": {"security": "S1", "securityError": {"code": 43, "category": "BAD_SEC", "message": "m"}}
        },
    )
    final = _msg(
        {
            "securityData": {"security": "S2", "securityError": {"code": 43, "category": "BAD_SEC", "message": "m"}}
        }
    )
    errors = extract_item_errors([partial, final])
    assert [e.security for e in errors] == ["S1", "S2"]


def test_shared_walker_accepts_both_shapes() -> None:
    payload_dict = {"results": {"security": "AAPL", "description": "Apple Inc (U.S.)"}}
    payload_list = {"results": [{"security": "AAPL"}, {"security": "MSFT"}]}
    assert len(walk_sequence([_msg(payload_dict)], "results")) == 1
    assert len(walk_sequence([_msg(payload_list)], "results")) == 2
    assert len(list(iter_sequence(payload_dict, "results"))) == 1
    assert len(list(iter_sequence(payload_list, "results"))) == 2


def test_no_errors_for_clean_data() -> None:
    payload = {"securityData": {"security": "AAPL US Equity", "fieldData": {"PX_LAST": 311.3}}}
    assert extract_item_errors([_msg(payload)]) == []
