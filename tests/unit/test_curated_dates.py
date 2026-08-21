"""Curated tool date normalization tests.

HistoricalDataRequest declares startDate/endDate as plain strings and
Bloomberg's historical server only parses YYYYMMDD; ISO dates must be
normalized by the curated layer.
"""

from bloomberg_mcp.mcp.curated_tools import _normalize_bbg_date


def test_iso_date_normalized() -> None:
    assert _normalize_bbg_date("2026-08-10") == "20260810"


def test_compact_date_passthrough() -> None:
    assert _normalize_bbg_date("20260810") == "20260810"


def test_none_yields_empty() -> None:
    assert _normalize_bbg_date(None) == ""


def test_non_date_string_untouched() -> None:
    assert _normalize_bbg_date("yesterday") == "yesterday"
    assert _normalize_bbg_date("") == ""


def test_partial_dash_not_mangled() -> None:
    # Not a full YYYY-MM-DD -> left alone for Bloomberg to reject/parse.
    assert _normalize_bbg_date("2026-08") == "2026-08"
