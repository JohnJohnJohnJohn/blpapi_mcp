"""Reference data normalizer (SPEC §3.6, §3.10)."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest
from bloomberg_mcp.normalization.registry import merged_sequence, stamp


class ReferenceNormalizer:
    schema_version = "bloomberg.reference/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in merged_sequence(messages, "securityData"):
            if not isinstance(entry, dict) or isinstance(entry.get("securityError"), dict):
                continue  # item errors are reported separately
            security = entry.get("security")
            field_data = entry.get("fieldData")
            if isinstance(field_data, dict):
                for field, value in field_data.items():
                    rows.append({"security": security, "field": field, "value": value})
        return stamp(
            request,
            self,
            {
                "columns": ["security", "field", "value"],
                "rows": rows,
                "untrusted_text_fields": [],
            },
        )
