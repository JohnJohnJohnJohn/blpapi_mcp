"""Subscription read cursors (SPEC §2.11, §3.9).

Cursors are opaque server-generated handles bound to a subscription group and
generation. Resubscription invalidates every cursor of the group; reading
with an invalidated cursor returns ``CURSOR_INVALID``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Cursor:
    cursor_id: str
    subscription_id: str
    generation: int
    offset: int  # sequence number of the next unread event


class CursorRegistry:
    def __init__(self) -> None:
        self._cursors: dict[str, Cursor] = {}

    def create(self, subscription_id: str, generation: int, offset: int) -> Cursor:
        cursor = Cursor(
            cursor_id=f"cur_{secrets.token_urlsafe(12)}",
            subscription_id=subscription_id,
            generation=generation,
            offset=offset,
        )
        self._cursors[cursor.cursor_id] = cursor
        return cursor

    def resolve(self, cursor_id: str) -> Cursor | None:
        return self._cursors.get(cursor_id)

    def consume(self, cursor: Cursor, new_offset: int) -> Cursor:
        """Advance a cursor to ``new_offset``, replacing the old handle."""
        self._cursors.pop(cursor.cursor_id, None)
        return self.create(cursor.subscription_id, cursor.generation, new_offset)

    def invalidate_subscription(self, subscription_id: str) -> None:
        for cursor_id in [c for c, cur in self._cursors.items() if cur.subscription_id == subscription_id]:
            self._cursors.pop(cursor_id, None)
