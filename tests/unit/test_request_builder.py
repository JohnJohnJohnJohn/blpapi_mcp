"""Native request builder regression tests (mock blpapi surface).

Verifies the builder emits the correct native element structure for scalars,
repeating scalars, and repeating sequences (e.g. ReferenceDataRequest
``overrides``), the path that regressed silently in the 2026-08-21 acceptance
run.
"""

from __future__ import annotations

import pytest

from bloomberg_mcp.blp.name_cache import NameCache
from bloomberg_mcp.blp.request_builder import populate_request
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import BloombergDatatype as D
from bloomberg_mcp.models import ElementDescriptor


class FakeElement:
    """Minimal stand-in for the blpapi Element surface used by the builder."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.value: object = None
        self.appended: list[FakeElement] = []
        self._members: dict[str, FakeElement] = {}

    def setValue(self, value: object) -> None:
        self.value = value

    def appendValue(self, value: object) -> None:
        self.appended.append(FakeElement(self.name))
        self.appended[-1].value = value

    def appendElement(self) -> FakeElement:
        child = FakeElement(self.name)
        self.appended.append(child)
        return child

    def getElement(self, name: str | object) -> FakeElement:
        key = str(name)
        member = self._members.get(key)
        if member is None:
            member = FakeElement(key)
            self._members[key] = member
        return member

    def setElement(self, name: str | object, value: object) -> None:
        member = self.getElement(name)
        member.value = value


class FakeRequest(FakeElement):
    pass


def _scalar(name: str, datatype: D, *, min_values: int = 0, max_values: int | None = 1) -> ElementDescriptor:
    return ElementDescriptor(name=name, datatype=datatype, min_values=min_values, max_values=max_values)


def _seq(
    name: str, children: tuple[ElementDescriptor, ...], *, max_values: int | None = None
) -> ElementDescriptor:
    return ElementDescriptor(name=name, datatype=D.SEQUENCE, max_values=max_values, children=children)


def _refdata_descriptor() -> ElementDescriptor:
    override = _seq("overrides", (_scalar("fieldId", D.STRING), _scalar("value", D.STRING)))
    return ElementDescriptor(
        name="ReferenceDataRequest",
        datatype=D.SEQUENCE,
        children=(
            _scalar("securities", D.STRING, min_values=1, max_values=None),
            _scalar("fields", D.STRING, min_values=1, max_values=None),
            _scalar("returnEids", D.BOOL),
            override,
        ),
    )


def test_repeating_sequence_override_built() -> None:
    request = FakeRequest("ReferenceDataRequest")
    populate_request(
        request,
        _refdata_descriptor(),
        {
            "securities": ["AAPL US Equity"],
            "fields": ["PX_LAST"],
            "returnEids": True,
            "overrides": [{"fieldId": "CURRENCY", "value": "JPY"}],
        },
        NameCache(),
    )
    assert request._members["securities"].appended[0].value == "AAPL US Equity"
    assert request._members["returnEids"].value is True
    overrides = request._members["overrides"]
    assert len(overrides.appended) == 1
    entry = overrides.appended[0]
    assert entry._members["fieldId"].value == "CURRENCY"
    assert entry._members["value"].value == "JPY"


def test_repeating_sequence_multiple_entries() -> None:
    request = FakeRequest("ReferenceDataRequest")
    populate_request(
        request,
        _refdata_descriptor(),
        {
            "securities": ["AAPL US Equity"],
            "fields": ["PX_LAST"],
            "overrides": [{"fieldId": "A", "value": "1"}, {"fieldId": "B", "value": "2"}],
        },
        NameCache(),
    )
    entries = request._members["overrides"].appended
    assert [e._members["fieldId"].value for e in entries] == ["A", "B"]
    assert [e._members["value"].value for e in entries] == ["1", "2"]


def test_unknown_element_raises() -> None:
    request = FakeRequest("ReferenceDataRequest")
    with pytest.raises(GatewayError) as excinfo:
        populate_request(
            request,
            _refdata_descriptor(),
            {"securities": ["AAPL US Equity"], "fields": ["PX_LAST"], "bogus": 1},
            NameCache(),
        )
    assert excinfo.value.code is ErrorCode.UNKNOWN_ELEMENT
