"""Historical data normalizer (SPEC §3.6).

Consumes the canonical decoder's output shape: HistoricalDataResponse sends one
message per security whose ``securityData`` is a single sequence object (dict),
and each ``fieldData`` row is column-wide (``{"date": ..., "PX_LAST": ...}``).
Rows are expanded to field-long form (security, date, field, value).
"""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest
from bloomberg_mcp.normalization.registry import stamp, walk_sequence


class HistoricalNormalizer:
    schema_version = "bloomberg.historical/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in walk_sequence(messages, "securityData"):
            if not isinstance(entry, dict) or isinstance(entry.get("securityError"), dict):
                continue  # item errors are reported separately
            security = entry.get("security")
            field_data = entry.get("fieldData")
            if not isinstance(field_data, list):
                continue
            for row in field_data:
                if not isinstance(row, dict):
                    continue
                date = row.get("date")
                for field, value in row.items():
                    if field == "date":
                        continue
                    rows.append(
                        {
                            "security": security,
                            # Calendar dates stay dates: no timezone-induced shifts.
                            "date": date,
                            "field": field,
                            "value": value,
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
