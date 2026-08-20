"""Conservative request-cost model (SPEC §4.4).

The model is not a latency predictor; it rejects clearly excessive
agent-generated requests before they reach Bloomberg.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from bloomberg_mcp.models import RequestCost

_BASE_COST = 1
_DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d")

_INTRADAY_INTERVAL_SECONDS = {
    "1": 60,
    "5": 300,
    "10": 600,
    "15": 900,
    "30": 1800,
    "60": 3600,
    "120": 7200,
}
_TRADING_DAY_SECONDS = 8 * 3600


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _string_list(parameters: Mapping[str, Any], key: str) -> list[str]:
    value = parameters.get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return []


def estimate_cost(operation: str, parameters: Mapping[str, Any]) -> RequestCost:
    """Estimate the cost of a canonical request.

    Securities/fields are looked up under the common Bloomberg element names;
    observation estimates use the request's own date range and periodicity so
    the estimate stays conservative without Bloomberg round-trips.
    """
    securities = _string_list(parameters, "securities") or _string_list(parameters, "security")
    fields = _string_list(parameters, "fields") or _string_list(parameters, "field")
    security_count = len(securities)
    field_count = len(fields)

    repeating = 0
    for value in parameters.values():
        if isinstance(value, list):
            repeating += len(value)

    observations = 0
    start = _parse_date(parameters.get("startDate"))
    end = _parse_date(parameters.get("endDate"))
    lowered = operation.lower()
    if "historical" in lowered and start is not None and end is not None and end >= start:
        days = (end - start).days + 1
        periodicity = str(parameters.get("periodicitySelection", "DAILY")).upper()
        divisor = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 30}.get(periodicity, 1)
        observations = security_count * field_count * max(1, days // divisor)
    elif "intradaybar" in lowered:
        interval = int(_INTRADAY_INTERVAL_SECONDS.get(str(parameters.get("interval", "1")), 60))
        bars_per_day = max(1, _TRADING_DAY_SECONDS // interval)
        observations = max(security_count, 1) * max(field_count, 1) * bars_per_day
    elif "intradaytick" in lowered:
        # Ticks are unbounded; assume one observation per second per field.
        observations = max(security_count, 1) * max(field_count, 1) * _TRADING_DAY_SECONDS

    risk = (
        _BASE_COST
        + security_count
        + field_count
        + repeating
        + (observations // 100)
    )

    return RequestCost(
        securities=security_count,
        fields=field_count,
        repeating_elements=repeating,
        estimated_observations=observations,
        risk_score=risk,
    )
