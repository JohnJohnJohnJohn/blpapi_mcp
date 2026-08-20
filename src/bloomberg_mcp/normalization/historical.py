"""Historical data normalizer (SPEC §3.6)."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest
from bloomberg_mcp.normalization.registry import merged_sequence, stamp


class HistoricalNormalizer:
    schema_version = "bloomberg.historical/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in merged_sequence(messages, "securityData"):
            if not isinstance(entry, dict) or isinstance(entry.get("securityError"), dict):
                continue
            security = entry.get("security")
            field_data = entry.get("fieldData")
            if not isinstance(field_data, list):
                continue
            for row in field_data:
                if not isinstance(row, dict):
                    continue
                rows.append(
                    {
                        "security": security,
                        # Calendar dates stay dates: no timezone-induced shifts.
                        "date": row.get("date"),
                        "field": row.get("field"),
                        "value": row.get("value"),
                    }
                )
        return stamp(
            request,
            self,
            {
                "columns": ["security", "date", "field", "value"],
                "rows": rows,
                "calendar_metadata": {"date_type": "calendar-date", "timezone": None},
                "untrusted_text_fields": [],
            },
        )
