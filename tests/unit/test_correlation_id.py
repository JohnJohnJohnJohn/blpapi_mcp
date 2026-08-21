"""Regression: blpapi.CorrelationId ignores keyword arguments.

Live discovery 2026-08-21: ``CorrelationId(value=token)`` yields an EMPTY
correlation id (type=UNSET, value=None), so blpapi auto-assigned its own id
for requests and subscriptions. Subscription events then carried an id that
never matched the gateway's token map, so every subscription stayed STARTING
while market data flowed. The token MUST be passed positionally.
"""

import blpapi


def test_positional_int_correlation_id_has_value() -> None:
    cid = blpapi.CorrelationId(7)
    assert cid.value() == 7
    assert cid.type() == blpapi.CorrelationId.INT_TYPE


def test_kwarg_correlation_id_is_empty() -> None:
    # The trap: the kwarg is silently ignored by the *argv/**kwargs ctor.
    cid = blpapi.CorrelationId(value=7)
    assert cid.value() is None
    assert cid.type() == blpapi.CorrelationId.UNSET_TYPE


def test_positional_string_correlation_id() -> None:
    cid = blpapi.CorrelationId("token-1")
    assert cid.value() == "token-1"
    # Not an INT id (no STRING_TYPE constant; string ids carry the value).
    assert cid.type() != blpapi.CorrelationId.INT_TYPE
