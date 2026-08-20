"""Instrument, curve and government-security search normalizers (SPEC §3.6)."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest
from bloomberg_mcp.normalization.registry import merged_sequence, stamp


class InstrumentSearchNormalizer:
    schema_version = "bloomberg.instrument-search/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in merged_sequence(messages, "results"):
            if isinstance(entry, dict):
                rows.append({"name": entry.get("name"), "yellow_key": entry.get("yellowKey")})
        # Instrument names originate from Bloomberg text and are untrusted.
        return stamp(
            request, self, {"columns": ["name", "yellow_key"], "rows": rows, "untrusted_text_fields": ["name"]}
        )


class CurveSearchNormalizer:
    schema_version = "bloomberg.curve-search/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in merged_sequence(messages, "curveList"):
            if isinstance(entry, dict):
                rows.append(
                    {"name": entry.get("name"), "country": entry.get("country"), "currency": entry.get("currency")}
                )
        return stamp(
            request,
            self,
            {"columns": ["name", "country", "currency"], "rows": rows, "untrusted_text_fields": ["name"]},
        )


class GovernmentSecuritySearchNormalizer:
    schema_version = "bloomberg.govt-search/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in merged_sequence(messages, "govtList"):
            if isinstance(entry, dict):
                rows.append({"name": entry.get("name"), "country": entry.get("country")})
        return stamp(
            request, self, {"columns": ["name", "country"], "rows": rows, "untrusted_text_fields": ["name"]}
        )
