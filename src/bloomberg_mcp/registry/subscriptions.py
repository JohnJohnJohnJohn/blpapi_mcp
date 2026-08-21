"""Subscription group registry (SPEC §2.11, §3.9).

One external group contains multiple Bloomberg subscription items; every
native item carries its own opaque native correlation token allocated by the
gateway. Native tokens are never returned externally. Buffers are bounded,
drops are counted, cursors are generation-bound, and long-polls are limited.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from bloomberg_mcp.blp.backend import BloombergBackend, SubscriptionEvent
from bloomberg_mcp.config import SubscriptionsConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import (
    SubscriptionGroup,
    SubscriptionGroupStatus,
    SubscriptionItem,
    SubscriptionItemStatus,
    utc_now,
)
from bloomberg_mcp.registry.cursors import CursorRegistry

logger = logging.getLogger(__name__)

_ACTIVE_GROUP_STATUSES = frozenset(
    {
        SubscriptionGroupStatus.CREATED,
        SubscriptionGroupStatus.STARTING,
        SubscriptionGroupStatus.ACTIVE,
        SubscriptionGroupStatus.DEGRADED,
        SubscriptionGroupStatus.RESUBSCRIBING,
    }
)


@dataclass
class BufferedEvent:
    item_id: str
    sequence: int
    received_at: str
    message_type: str
    payload: Mapping[str, Any]


@dataclass
class _GroupRuntime:
    group: SubscriptionGroup
    buffer: deque[BufferedEvent] = field(default_factory=deque)
    total_events: int = 0
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)
    waiters: list[asyncio.Future[None]] = field(default_factory=list)
    max_events: int = 10_000
    ttl_seconds: int = 3_600
    read_mode: str = "latest_and_changes"


class SubscriptionRegistry:
    def __init__(
        self,
        backend: BloombergBackend,
        config: SubscriptionsConfig,
        cursors: CursorRegistry,
        subscription_service: str = "//blp/mktdata",
    ) -> None:
        self._backend = backend
        self._config = config
        self._cursors = cursors
        self._subscription_service = subscription_service
        self._groups: dict[str, _GroupRuntime] = {}
        self._token_map: dict[int, tuple[str, str]] = {}
        self._pending_unsubscribes: set[int] = set()
        self._token_counter = 0
        self._long_polls = asyncio.Semaphore(config.maximum_concurrent_long_polls)
        backend.set_subscription_sink(self.handle_event)

    # ------------------------------------------------------------------ create

    def _allocate_token(self) -> int:
        self._token_counter += 1
        return self._token_counter

    async def create(
        self,
        principal_id: str,
        subscriptions: Sequence[Mapping[str, Any]],
        retention: Mapping[str, Any] | None,
    ) -> SubscriptionGroup:
        active = [
            rt for rt in self._groups.values() if rt.group.status in _ACTIVE_GROUP_STATUSES
            and rt.group.principal_id == principal_id
        ]
        if len(active) >= self._config.maximum_per_principal:
            raise GatewayError(ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED, "Subscription group limit reached.")
        if not subscriptions:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "At least one subscription item is required.")
        if len(subscriptions) > self._config.maximum_topics_per_group:
            raise GatewayError(ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED, "Too many topics in one group.")

        max_events = self._config.maximum_buffered_events
        ttl_seconds = self._config.default_ttl_seconds
        read_mode = "latest_and_changes"
        if retention:
            requested_events = retention.get("max_events")
            if isinstance(requested_events, int) and requested_events > 0:
                max_events = min(requested_events, self._config.maximum_buffered_events)
            requested_ttl = retention.get("ttl_seconds")
            if isinstance(requested_ttl, int) and requested_ttl > 0:
                ttl_seconds = min(requested_ttl, self._config.maximum_ttl_seconds)
            requested_mode = retention.get("mode")
            if requested_mode is not None:
                if requested_mode not in ("latest_and_changes", "changes_only", "latest_only"):
                    raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Unknown retention mode {requested_mode!r}.")
                read_mode = requested_mode

        group = SubscriptionGroup(
            subscription_id=f"sub_{secrets.token_urlsafe(12)}",
            principal_id=principal_id,
            generation=1,
            status=SubscriptionGroupStatus.STARTING,
            created_at=utc_now(),
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )
        items_payload: list[dict[str, Any]] = []
        tokens: list[int] = []
        for spec in subscriptions:
            topic = str(spec.get("topic") or "")
            if not topic:
                raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Subscription topic must be non-empty.")
            fields = tuple(str(f) for f in (spec.get("fields") or []))
            if len(fields) > self._config.maximum_fields_per_topic:
                raise GatewayError(ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED, "Too many fields for one topic.")
            options = {str(k): str(v) for k, v in (spec.get("options") or {}).items()}
            token = self._allocate_token()
            item = SubscriptionItem(
                item_id=f"subitem_{secrets.token_urlsafe(12)}",
                topic=topic,
                fields=fields,
                options=options,
                native_token=token,
                status=SubscriptionItemStatus.STARTING,
            )
            group.items[item.item_id] = item
            self._token_map[token] = (group.subscription_id, item.item_id)
            items_payload.append({"topic": topic, "fields": list(fields), "options": options})
            tokens.append(token)

        runtime = _GroupRuntime(group=group, max_events=max_events, ttl_seconds=ttl_seconds, read_mode=read_mode)
        self._groups[group.subscription_id] = runtime
        try:
            await self._backend.subscribe(items_payload, tokens)
        except Exception:
            self._groups.pop(group.subscription_id, None)
            for token in tokens:
                self._token_map.pop(token, None)
            raise
        return group

    # ------------------------------------------------------------------- events

    async def handle_event(self, event: SubscriptionEvent) -> None:
        location = self._token_map.get(event.native_token)
        if location is None:
            # Stale event from a retired token / previous generation (SPEC §2.6).
            return
        subscription_id, item_id = location
        runtime = self._groups.get(subscription_id)
        if runtime is None:
            return
        group = runtime.group
        item = group.items.get(item_id)
        if item is None:
            return

        if event.status == "SUBSCRIPTION_STARTED":
            item.status = SubscriptionItemStatus.ACTIVE
            self._recompute_group_status(runtime)
            self._wake(runtime)
            return
        if event.status in ("SUBSCRIPTION_FAILURE", "SUBSCRIPTION_TERMINATED"):
            item.status = SubscriptionItemStatus.FAILED
            self._recompute_group_status(runtime)
            self._wake(runtime)
            return
        if event.status in ("UNSUBSCRIBED",):
            item.status = SubscriptionItemStatus.CANCELLED
            self._recompute_group_status(runtime)
            self._wake(runtime)
            return

        item.sequence += 1
        runtime.total_events += 1
        if len(runtime.buffer) >= runtime.max_events:
            runtime.buffer.popleft()
            group.dropped_events += 1
        runtime.buffer.append(
            BufferedEvent(
                item_id=item_id,
                sequence=item.sequence,
                received_at=event.received_at,
                message_type=event.message_type,
                payload=event.payload,
            )
        )
        latest = runtime.latest.setdefault(item_id, {})
        latest.update(event.payload)
        self._wake(runtime)

    def _warn_if_stuck_starting(self, runtime: _GroupRuntime) -> None:
        """Surface a diagnostic when a group never transitions out of STARTING.

        A liquid topic (e.g. EURUSD Curncy) that receives no SUBSCRIPTION_STARTED
        event for a sustained period means the native session never confirmed the
        subscription; the log line names the group for server-side correlation.
        """
        group = runtime.group
        if group.status is not SubscriptionGroupStatus.STARTING:
            return
        age = (utc_now() - group.created_at).total_seconds()
        if age >= self._config.maximum_long_poll_seconds:
            logger.warning(
                "subscription %s still STARTING after %.0fs (topic=%s, item_statuses=%s); "
                "no native activation event received",
                group.subscription_id,
                age,
                [item.topic for item in group.items.values()],
                [item.status.value for item in group.items.values()],
            )

    def _recompute_group_status(self, runtime: _GroupRuntime) -> None:
        statuses = {item.status for item in runtime.group.items.values()}
        if statuses == {SubscriptionItemStatus.ACTIVE}:
            runtime.group.status = SubscriptionGroupStatus.ACTIVE
        elif SubscriptionItemStatus.ACTIVE in statuses or statuses == {SubscriptionItemStatus.STARTING}:
            runtime.group.status = (
                SubscriptionGroupStatus.STARTING
                if statuses <= {SubscriptionItemStatus.STARTING}
                else SubscriptionGroupStatus.DEGRADED
            )
        elif statuses == {SubscriptionItemStatus.FAILED}:
            runtime.group.status = SubscriptionGroupStatus.FAILED

    def _wake(self, runtime: _GroupRuntime) -> None:
        for waiter in runtime.waiters:
            if not waiter.done():
                waiter.set_result(None)
        runtime.waiters.clear()

    # -------------------------------------------------------------------- read

    async def read(
        self,
        principal_id: str,
        subscription_id: str,
        generation: int | None,
        mode: str,
        cursor_id: str | None,
        limit: int,
        wait_seconds: float,
        *,
        admin: bool = False,
    ) -> dict[str, Any]:
        runtime = self._require_owned(principal_id, subscription_id, admin)
        group = runtime.group
        if group.status in (SubscriptionGroupStatus.CANCELLED, SubscriptionGroupStatus.EXPIRED):
            raise GatewayError(ErrorCode.SUBSCRIPTION_EXPIRED, "Subscription is no longer active.")
        if generation is not None and generation != group.generation:
            raise GatewayError(ErrorCode.CURSOR_INVALID, "Subscription generation has changed.")
        if mode == "latest" and runtime.read_mode == "changes_only":
            raise GatewayError(
                ErrorCode.INVALID_ARGUMENT, "This subscription retains changes only (retention.mode=changes_only)."
            )
        if mode == "changes" and runtime.read_mode == "latest_only":
            raise GatewayError(
                ErrorCode.INVALID_ARGUMENT, "This subscription retains latest values only (retention.mode=latest_only)."
            )
        self._warn_if_stuck_starting(runtime)

        if mode == "latest":
            return {
                "subscription_id": subscription_id,
                "generation": group.generation,
                "mode": "latest",
                "latest": {
                    item_id: dict(values) for item_id, values in runtime.latest.items()
                },
                "dropped_events": group.dropped_events,
                "cursor": None,
            }

        if mode != "changes":
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Unknown read mode {mode!r}.")

        offset = 0
        cursor = None
        if cursor_id is not None:
            cursor = self._cursors.resolve(cursor_id, principal_id=principal_id)
            if cursor is None or cursor.subscription_id != subscription_id or cursor.generation != group.generation:
                raise GatewayError(ErrorCode.CURSOR_INVALID, "Cursor is invalid for this subscription generation.")
            offset = cursor.offset

        if wait_seconds > 0:
            if self._long_polls.locked():
                raise GatewayError(ErrorCode.RATE_LIMITED, "Long-poll limit reached; retry shortly.", retryable=True)
            capped = min(wait_seconds, float(self._config.maximum_long_poll_seconds))
            async with self._long_polls:
                await self._wait_for_events(runtime, offset, capped)

        buffered = list(runtime.buffer)
        base = runtime.total_events - len(buffered)
        selected: list[tuple[int, BufferedEvent]] = [
            (base + position, e) for position, e in enumerate(buffered) if base + position >= offset
        ]
        page = selected[: max(1, min(limit, 1000))]
        events = [
            {
                "item_id": e.item_id,
                "sequence": e.sequence,
                "received_at": e.received_at,
                "message_type": e.message_type,
                "payload": dict(e.payload),
            }
            for _, e in page
        ]
        new_offset = (page[-1][0] + 1) if page else offset
        # Consume-on-use: the old handle is replaced atomically; replaying it
        # afterwards fails with CURSOR_INVALID. Explicit data-gap reporting:
        # if the requested offset fell outside the retained window, say so.
        gap = offset < base
        if cursor is not None:
            new_cursor = self._cursors.consume(
                cursor,
                new_offset,
                ttl_seconds=self._config.cursor_ttl_seconds,
            )
        else:
            new_cursor = self._cursors.create(
                subscription_id,
                group.generation,
                new_offset,
                principal_id=principal_id,
                ttl_seconds=self._config.cursor_ttl_seconds,
            )
        return {
            "subscription_id": subscription_id,
            "generation": group.generation,
            "mode": "changes",
            "events": events,
            "dropped_events": group.dropped_events,
            "cursor": new_cursor.cursor_id,
            "data_gap": gap,
        }

    async def _wait_for_events(self, runtime: _GroupRuntime, offset: int, timeout_seconds: float) -> None:
        if runtime.total_events > offset:
            return
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        runtime.waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout_seconds)
        except TimeoutError:
            pass
        finally:
            if waiter in runtime.waiters:
                runtime.waiters.remove(waiter)

    # -------------------------------------------------------------- resubscribe

    async def resubscribe(
        self,
        principal_id: str,
        subscription_id: str,
        subscriptions: Sequence[Mapping[str, Any]],
        *,
        admin: bool = False,
    ) -> SubscriptionGroup:
        runtime = self._require_owned(principal_id, subscription_id, admin)
        group = runtime.group
        if group.status in (SubscriptionGroupStatus.CANCELLED, SubscriptionGroupStatus.EXPIRED):
            raise GatewayError(ErrorCode.SUBSCRIPTION_EXPIRED, "Subscription is no longer active.")
        if len(subscriptions) > self._config.maximum_topics_per_group:
            raise GatewayError(ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED, "Too many topics in one group.")

        # Phase 1 — validate and construct the prospective state WITHOUT
        # touching the live group (transactional: a failed replacement must
        # leave the old subscription fully intact).
        prospective_items: list[tuple[SubscriptionItem, int]] = []
        items_payload: list[dict[str, Any]] = []
        tokens: list[int] = []
        for spec in subscriptions:
            topic = str(spec.get("topic") or "")
            if not topic:
                raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Subscription topic must be non-empty.")
            fields = tuple(str(f) for f in (spec.get("fields") or []))
            if len(fields) > self._config.maximum_fields_per_topic:
                raise GatewayError(ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED, "Too many fields for one topic.")
            options = {str(k): str(v) for k, v in (spec.get("options") or {}).items()}
            token = self._allocate_token()
            item = SubscriptionItem(
                item_id=f"subitem_{secrets.token_urlsafe(12)}",
                topic=topic,
                fields=fields,
                options=options,
                native_token=token,
                status=SubscriptionItemStatus.STARTING,
            )
            prospective_items.append((item, token))
            items_payload.append({"topic": topic, "fields": list(fields), "options": options})
            tokens.append(token)

        # Phase 2 — subscribe the NEW native feeds before retiring the old ones.
        try:
            await self._backend.subscribe(items_payload, tokens)
        except Exception as exc:
            # Rollback: release any partially-subscribed new tokens; the old
            # group state is untouched.
            try:
                await self._backend.unsubscribe(tokens)
            except Exception:
                logger.warning("rollback unsubscribe failed for %s", subscription_id, exc_info=True)
            for token in tokens:
                self._token_map.pop(token, None)
            raise GatewayError(
                ErrorCode.BLOOMBERG_SUBSCRIPTION_FAILED,
                "Bloomberg rejected the resubscription request.",
                retryable=True,
            ) from exc

        # Phase 3 — commit: swap items, retire old native feeds (async teardown).
        old_tokens = [item.native_token for item in group.items.values()]
        group.generation += 1
        group.status = SubscriptionGroupStatus.STARTING
        group.items.clear()
        for old_token in old_tokens:
            self._token_map.pop(old_token, None)
        if old_tokens:
            self._pending_unsubscribes.update(old_tokens)
        group.restored_with_gap = True
        runtime.buffer.clear()
        runtime.latest.clear()
        runtime.total_events = 0
        self._cursors.invalidate_subscription(subscription_id)
        for item, token in prospective_items:
            group.items[item.item_id] = item
            self._token_map[token] = (group.subscription_id, item.item_id)
        self._recompute_group_status(runtime)
        return group

    # ------------------------------------------------------------------- cancel

    async def cancel(self, principal_id: str, subscription_id: str, *, admin: bool = False) -> SubscriptionGroup:
        runtime = self._require_owned(principal_id, subscription_id, admin)
        group = runtime.group
        if group.status == SubscriptionGroupStatus.CANCELLED:
            return group  # idempotent
        tokens = [item.native_token for item in group.items.values()]
        try:
            await self._backend.unsubscribe(tokens)
        except Exception:
            # Native teardown failed: keep it observable and retryable via the
            # pending-unsubscribe set instead of swallowing it silently.
            logger.warning("unsubscribe during cancel failed for %s; will retry", subscription_id, exc_info=True)
            self._pending_unsubscribes.update(tokens)
        for token in tokens:
            self._token_map.pop(token, None)
        for item in group.items.values():
            item.status = SubscriptionItemStatus.CANCELLED
        group.status = SubscriptionGroupStatus.CANCELLED
        self._cursors.invalidate_subscription(subscription_id)
        self._wake(runtime)
        return group

    # -------------------------------------------------------------------- misc

    def _require_owned(self, principal_id: str, subscription_id: str, admin: bool) -> _GroupRuntime:
        runtime = self._groups.get(subscription_id)
        if runtime is None:
            raise GatewayError(ErrorCode.SUBSCRIPTION_NOT_FOUND, "Subscription not found.")
        if runtime.group.principal_id != principal_id and not admin:
            # Cross-principal access is denied with the same error as absence.
            raise GatewayError(ErrorCode.SUBSCRIPTION_NOT_FOUND, "Subscription not found.")
        return runtime

    def list_groups(self, principal_id: str, *, admin: bool = False) -> list[SubscriptionGroup]:
        groups = []
        for runtime in self._groups.values():
            if admin or runtime.group.principal_id == principal_id:
                groups.append(runtime.group)
        return groups

    def active_group_count(self, principal_id: str) -> int:
        return len(
            [
                rt
                for rt in self._groups.values()
                if rt.group.principal_id == principal_id and rt.group.status in _ACTIVE_GROUP_STATUSES
            ]
        )

    async def expire_due(self) -> list[str]:
        """Expire groups past their TTL, attempting native unsubscribe first.

        Native teardown failures stay observable and retryable via
        ``_pending_unsubscribes`` instead of being swallowed.
        """
        now = utc_now()
        expired: list[str] = []
        for subscription_id, runtime in list(self._groups.items()):
            group = runtime.group
            if group.expires_at is not None and now >= group.expires_at and group.status in _ACTIVE_GROUP_STATUSES:
                tokens = [item.native_token for item in group.items.values()]
                try:
                    await self._backend.unsubscribe(tokens)
                except Exception:
                    logger.warning(
                        "unsubscribe during expiry failed for %s; will retry",
                        subscription_id,
                        exc_info=True,
                    )
                    self._pending_unsubscribes.update(tokens)
                group.status = SubscriptionGroupStatus.EXPIRED
                for item in group.items.values():
                    item.status = SubscriptionItemStatus.EXPIRED
                for token in tokens:
                    self._token_map.pop(token, None)
                self._cursors.invalidate_subscription(subscription_id)
                expired.append(subscription_id)
        return expired

    async def retry_pending_unsubscribes(self) -> int:
        """Retry native unsubscribes that previously failed (cancel/expiry)."""
        if not self._pending_unsubscribes:
            return 0
        tokens = list(self._pending_unsubscribes)
        succeeded = 0
        for token in tokens:
            try:
                await self._backend.unsubscribe([token])
            except Exception:
                logger.warning("pending unsubscribe retry failed for token %d", token, exc_info=True)
                continue
            self._pending_unsubscribes.discard(token)
            succeeded += 1
        return succeeded

    async def restore_after_reconnect(self) -> None:
        """Re-establish active groups after session recovery (SPEC §2.6).

        Restored subscriptions receive a new generation and a data-gap flag;
        old cursors are invalidated.
        """
        for runtime in list(self._groups.values()):
            group = runtime.group
            if group.status not in _ACTIVE_GROUP_STATUSES:
                continue
            old_tokens = [item.native_token for item in group.items.values()]
            for token in old_tokens:
                self._token_map.pop(token, None)
            group.generation += 1
            group.restored_with_gap = True
            runtime.buffer.clear()
            runtime.latest.clear()
            runtime.total_events = 0
            self._cursors.invalidate_subscription(group.subscription_id)
            items_payload: list[dict[str, Any]] = []
            tokens: list[int] = []
            for item in group.items.values():
                token = self._allocate_token()
                item.native_token = token
                item.status = SubscriptionItemStatus.STARTING
                item.sequence = 0
                self._token_map[token] = (group.subscription_id, item.item_id)
                items_payload.append({"topic": item.topic, "fields": list(item.fields), "options": dict(item.options)})
                tokens.append(token)
            group.status = SubscriptionGroupStatus.STARTING
            try:
                await self._backend.subscribe(items_payload, tokens)
            except Exception:
                logger.warning("subscription restore failed for %s", group.subscription_id)
                # Drop the dangling token mappings for the never-established
                # native feeds (finding E5) so no stale routing remains.
                for token in tokens:
                    self._token_map.pop(token, None)
                group.status = SubscriptionGroupStatus.FAILED
