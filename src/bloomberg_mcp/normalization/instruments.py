"""Instrument, curve and government-security search normalizers (SPEC §3.6).

The canonical decoder emits every search response's result array under
``results`` (verified live 2026-08-21): instrument results carry
``security``/``description``, curve results carry ``curve``/``description``/
``country``/``currency``/..., government results carry ``parseky``/``name``/
``ticker``. The yellow key (e.g. ``equity``) is embedded in the instrument
security string as ``TICKER<yellow_key>``.
"""

from __future__ import annotations

import re

from typing import Any

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest
from bloomberg_mcp.normalization.registry import stamp, walk_sequence

_YELLOW_KEY_RE = re.compile(r"<([^>]+)>")


def _yellow_key(security: Any) -> str | None:
    if not isinstance(security, str):
        return None
    match = _YELLOW_KEY_RE.search(security)
    return match.group(1) if match else None


class InstrumentSearchNormalizer:
    schema_version = "bloomberg.instrument-search/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in walk_sequence(messages, "results"):
            if not isinstance(entry, dict):
                continue
            security = entry.get("security")
            rows.append(
                {
                    "name": entry.get("description") or security,
                    "yellow_key": _yellow_key(security),
                }
            )
        # Instrument names originate from Bloomberg text and are untrusted.
        return stamp(
            request, self, {"columns": ["name", "yellow_key"], "rows": rows, "untrusted_text_fields": ["name"]}
        )


class CurveSearchNormalizer:
    schema_version = "bloomberg.curve-search/1"

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for entry in walk_sequence(messages, "results"):
            if not isinstance(entry, dict):
                continue
            rows.append(
                {
                    "name": entry.get("curve") or entry.get("description"),
                    "country": entry.get("country"),
                    "currency": entry.get("currency"),
                }
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
        for entry in walk_sequence(messages, "results"):
            if not isinstance(entry, dict):
                continue
            # The decoded govtList results carry parseky/name/ticker; no
            # country element is present in the response payload.
            rows.append(
                {
                    "name": entry.get("name") or entry.get("parseky"),
                    "country": entry.get("country"),
                }
            )
        return stamp(
            request,
            self,
            {"columns": ["name", "country"], "rows": rows, "untrusted_text_fields": ["name"]},
        )
