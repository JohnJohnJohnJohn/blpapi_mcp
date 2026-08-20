"""Field search normalizer (SPEC §3.6)."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest
from bloomberg_mcp.normalization.registry import merged_sequence, stamp


class FieldSearchNormalizer:
    schema_version = "bloomberg.field-search/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in merged_sequence(messages, "fieldData"):
            if not isinstance(entry, dict):
                continue
            info = entry.get("fieldInfo")
            if isinstance(info, dict):
                rows.append(
                    {
                        "mnemonic": info.get("mnemonic"),
                        "description": info.get("description"),
                        "field_type": info.get("fieldType"),
                    }
                )
        # Descriptions are Bloomberg-authored text: marked untrusted.
        return stamp(
            request,
            self,
            {
                "columns": ["mnemonic", "description", "field_type"],
                "rows": rows,
                "untrusted_text_fields": ["description"],
            },
        )
