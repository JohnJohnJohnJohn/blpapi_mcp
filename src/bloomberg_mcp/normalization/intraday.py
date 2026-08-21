"""Intraday bar and tick normalizers (SPEC §3.6).

The canonical decoder emits ``barData`` / ``tickData`` as single sequence
objects (dicts, one per message); bar/tick rows live under ``barTickData`` /
``tickData``. The request is single-security, so the security label comes from
the request parameters.
"""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest
from bloomberg_mcp.normalization.registry import stamp, walk_sequence


class IntradayBarNormalizer:
    schema_version = "bloomberg.intraday-bar/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        security = request.parameters.get("security")
        for entry in walk_sequence(messages, "barData"):
            if not isinstance(entry, dict) or isinstance(entry.get("securityError"), dict):
                continue
            for bar in entry.get("barTickData") or []:
                if not isinstance(bar, dict):
                    continue
                rows.append({"security": security, **bar})
        return stamp(
            request,
            self,
            {
                "columns": ["security", "time", "open", "high", "low", "close", "volume"],
                "rows": rows,
                "untrusted_text_fields": [],
            },
        )


class IntradayTickNormalizer:
    schema_version = "bloomberg.intraday-tick/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        security = request.parameters.get("security")
        for entry in walk_sequence(messages, "tickData"):
            if not isinstance(entry, dict) or isinstance(entry.get("securityError"), dict):
                continue
            for tick in entry.get("tickData") or []:
                if not isinstance(tick, dict):
                    continue
                rows.append({"security": security, **tick})
        return stamp(
            request,
            self,
            {
                "columns": ["security", "time", "type", "value", "size"],
                "rows": rows,
                "untrusted_text_fields": [],
            },
        )
