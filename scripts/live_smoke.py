#!/usr/bin/env python3
"""Live subscription smoke test for the Bloomberg MCP Gateway.

Bounded and read-only: subscribes to EURUSD Curncy (3 fields), waits up to
``wait_seconds`` (default 25, capped 30) for at least one market-data event,
then cancels. Exits 0 on success, 1 on failure. Prints one JSON summary line.

Usage:
    BLOOMBERG_MCP_URL=http://host:8775/mcp BLOOMBERG_MCP_TOKEN=... python scripts/live_smoke.py
    BLOOMBERG_MCP_URL=... BLOOMBERG_MCP_TOKEN=... python scripts/live_smoke.py --wait 25 --timeout 60

This is the regression guard for the correlation-id routing fix: a subscription
that never receives events (or never leaves STARTING) fails the smoke test.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

_URL = (os.environ.get("BLOOMBERG_MCP_URL", "") or "").rstrip("/")
_TOKEN = os.environ.get("BLOOMBERG_MCP_TOKEN", "")
_PROTO = os.environ.get("BLOOMBERG_MCP_PROTOCOL", "2026-07-28")


def _parse_body(body: str) -> dict:
    """MCP streamable HTTP may return JSON or SSE-framed JSON-RPC."""
    if body.lstrip().startswith("event:") or "data:" in body[:64]:
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
    return json.loads(body)


def rpc(endpoint: str, token: str, method: str, params: dict, rid: int) -> dict:
    payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": {
        "_meta": {"io.modelcontextprotocol/protocolVersion": _PROTO, "io.modelcontextprotocol/clientCapabilities": {}},
        **params,
    }}
    body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": _PROTO,
        "mcp-method": method,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if "name" in params:
        headers["mcp-name"] = params["name"]
    req = urllib.request.Request(endpoint, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _parse_body(resp.read().decode("utf-8", "replace"))



def sc(msg: dict) -> dict:
    inner = msg.get("result")
    if isinstance(inner, dict):
        return inner.get("structuredContent") or {}
    return msg.get("structuredContent") or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=25, help="seconds to wait for events (<=30)")
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()
    wait = max(1, min(args.wait, 30))

    if not _URL or not _TOKEN:
        print(json.dumps({"ok": False, "error": "BLOOMBERG_MCP_URL and BLOOMBERG_MCP_TOKEN are required"}))
        return 1

    rid = 100
    created = sc(rpc(_URL, _TOKEN, "tools/call", {"name": "blpapi_subscribe", "arguments": {
        "subscriptions": [{"topic": "EURUSD Curncy", "fields": ["LAST_PRICE", "BID", "ASK"]}],
        "retention": {"mode": "latest_and_changes", "max_events": 200, "ttl_seconds": 120},
    }}, rid))
    rid += 1
    data = created.get("data") or {}
    if not created.get("ok"):
        print(json.dumps({"ok": False, "stage": "subscribe", "error": created.get("error")}))
        return 1
    sub_id = data.get("subscription_id") or data.get("group_id")
    if not sub_id:
        print(json.dumps({"ok": False, "stage": "subscribe", "error": "no subscription_id in response"}))
        return 1

    try:
        deadline = time.monotonic() + wait
        events: list = []
        cursor: str | None = None
        rd: dict = {}
        while time.monotonic() < deadline:
            read = sc(rpc(_URL, _TOKEN, "tools/call", {"name": "blpapi_read_subscription", "arguments": {
                "subscription_id": sub_id, "mode": "changes", "limit": 50, "wait_seconds": 8,
                **({"cursor": cursor} if cursor else {}),
            }}, rid))
            rid += 1
            rd = read.get("data") or {}
            events.extend(rd.get("events") or [])
            cursor = rd.get("cursor") or cursor
            if events:
                break
            time.sleep(1.0)
        ok = bool(events)
        print(json.dumps({
            "ok": ok,
            "stage": "read",
            "subscription_id": sub_id,
            "events": len(events),
            "first_event": (events[0].get("message_type") if events else None),
            "generation": rd.get("generation"),
            "cursor": cursor,
        }))
        return 0 if ok else 1
    finally:
        try:
            rpc(_URL, _TOKEN, "tools/call", {"name": "blpapi_cancel_subscription", "arguments": {"subscription_id": sub_id}}, rid)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass


if __name__ == "__main__":
    sys.exit(main())
