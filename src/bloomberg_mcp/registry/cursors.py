"""Subscription read cursors (SPEC §2.11, §3.9).

Cursors are opaque server-generated handles bound to a subscription group and
generation. Resubscription invalidates every cursor of the group; reading
with an invalidated cursor returns ``CURSOR_INVALID``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from bloomberg_mcp.models import utc_now


@dataclass(frozen=True)
class Cursor:
    cursor_id: str
    subscription_id: str
    generation: int
    offset: int  # sequence number of the next unread event
    principal_id: str | None = None
    expires_at: datetime | None = None


class CursorRegistry:
    """Bounded, TTL'd, principal-bound cursor store with consume-on-use."""

    MAX_CURSORS = 10_000

    def __init__(self) -> None:
        self._cursors: dict[str, Cursor] = {}

    def create(
        self,
        subscription_id: str,
        generation: int,
        offset: int,
        *,
        principal_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> Cursor:
        cursor = Cursor(
            cursor_id=f"cur_{secrets.token_urlsafe(12)}",
            subscription_id=subscription_id,
            generation=generation,
            offset=offset,
            principal_id=principal_id,
            expires_at=utc_now() + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        )
        self._cursors[cursor.cursor_id] = cursor
        self._evict_if_needed()
        return cursor

    def _evict_if_needed(self) -> None:
        if len(self._cursors) <= self.MAX_CURSORS:
            return
        # Drop expired first, then the oldest remaining handles.
        now = utc_now()
        for cid in [
            cid
            for cid, cur in self._cursors.items()
            if cur.expires_at is not None and now >= cur.expires_at
        ]:
            self._cursors.pop(cid, None)
        overflow = len(self._cursors) - self.MAX_CURSORS
        if overflow > 0:
            for cid, _ in sorted(self._cursors.items(), key=lambda pair: pair[1].expires_at or utc_now())[:overflow]:
                self._cursors.pop(cid, None)

    def resolve(self, cursor_id: str, principal_id: str | None = None) -> Cursor | None:
        cursor = self._cursors.get(cursor_id)
        if cursor is None:
            return None
        if cursor.expires_at is not None and utc_now() >= cursor.expires_at:
            self._cursors.pop(cursor_id, None)
            return None
        if principal_id is not None and cursor.principal_id is not None and cursor.principal_id != principal_id:
            self._cursors.pop(cursor_id, None)
            return None
        return cursor

    def consume(self, cursor: Cursor, new_offset: int, *, ttl_seconds: int | None = None) -> Cursor:
        """Advance a cursor, atomically replacing the old handle (consume-on-use)."""
        self._cursors.pop(cursor.cursor_id, None)
        return self.create(
            cursor.subscription_id,
            cursor.generation,
            new_offset,
            principal_id=cursor.principal_id,
            ttl_seconds=ttl_seconds,
        )

    def invalidate_subscription(self, subscription_id: str) -> None:
        for cursor_id in [c for c, cur in self._cursors.items() if cur.subscription_id == subscription_id]:
            self._cursors.pop(cursor_id, None)
