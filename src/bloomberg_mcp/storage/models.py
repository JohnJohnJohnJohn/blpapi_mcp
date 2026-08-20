"""Result store models shared by the memory and file backends (SPEC §4.7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ArtifactInfo:
    result_id: str
    principal_id: str
    representation: str  # canonical-events | typed-events | normalized | metadata
    format: str  # json | jsonl | parquet | arrow
    content_type: str
    byte_count: int
    message_count: int
    sha256: str
    expires_at: datetime
    backend: str  # memory | file

    def resource_uri(self, kind: str = "metadata") -> str:
        return f"bloomberg-result://{self.result_id}/{kind}"

    def to_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "resource_uri": self.resource_uri(),
            "representation": self.representation,
            "format": self.format,
            "content_type": self.content_type,
            "byte_count": self.byte_count,
            "message_count": self.message_count,
            "sha256": self.sha256,
            "expires_at": self.expires_at.isoformat(),
        }
