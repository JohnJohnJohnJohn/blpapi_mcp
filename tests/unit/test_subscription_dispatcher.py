"""Subscription dispatcher correlation-id routing tests.

Regression for the 2026-08-21 live failure: MarketDataEvents arrived with
correlation ids that are not INT_TYPE, and the type filter dropped every
event (subscriptions stayed STARTING while data flowed). _extract_tokens is
now type-agnostic.
"""

from bloomberg_mcp.blp.subscription_dispatcher import _extract_tokens


class _FakeCorrelationId:
    def __init__(self, value: object) -> None:
        self._value = value

    def value(self) -> object:
        return self._value


class _FakeMessage:
    def __init__(self, cids: list[object]) -> None:
        self._cids = [_FakeCorrelationId(v) for v in cids]

    def correlationIds(self) -> list[_FakeCorrelationId]:
        return self._cids


def test_int_correlation_ids_pass_through() -> None:
    message = _FakeMessage([5, 7])
    assert _extract_tokens(message) == [5, 7]


def test_stringified_numeric_ids_coerced() -> None:
    # Live shape: MarketDataEvents cids are not INT_TYPE; their values coerce.
    message = _FakeMessage(["5", "7"])
    assert _extract_tokens(message) == [5, 7]


def test_mixed_ids_all_coerced() -> None:
    message = _FakeMessage([5, "7", 9.0])
    assert _extract_tokens(message) == [5, 7, 9]


def test_uncoercible_ids_skipped() -> None:
    message = _FakeMessage([5, "nope", None, "12"])
    assert _extract_tokens(message) == [5, 12]


def test_no_ids_yields_empty() -> None:
    message = _FakeMessage([])
    assert _extract_tokens(message) == []
