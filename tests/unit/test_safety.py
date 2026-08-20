"""Audit redaction, storage safety and instance locking (SPEC §4.7-§4.8, §1.5)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest

from bloomberg_mcp.config import AuditConfig
from bloomberg_mcp.instance_lock import InstanceLock
from bloomberg_mcp.models import utc_now
from bloomberg_mcp.observability.audit import AuditEvent, AuditLogger, hash_client_request_id
from bloomberg_mcp.storage.file_store import FileStore
from bloomberg_mcp.storage.models import ArtifactInfo


def _capture(config: AuditConfig) -> tuple[AuditLogger, list[str]]:
    records: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("bloomberg_mcp.audit")
    handler = Handler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return AuditLogger(config), records


def test_audit_redacts_by_default() -> None:
    audit, records = _capture(AuditConfig())
    try:
        audit.record(
            AuditEvent(
                action="request",
                principal_id="hermes",
                extra={"securities": ["700 HK Equity"], "fields": ["PX_LAST"], "parameters": {"x": 1}},
            )
        )
    finally:
        logging.getLogger("bloomberg_mcp.audit").handlers.clear()
    payload = json.loads(records[-1])
    assert "securities" not in payload["extra"]
    assert "parameters" not in payload["extra"]
    assert payload["extra"].get("fields") == ["PX_LAST"]


def test_audit_includes_when_configured() -> None:
    audit, records = _capture(
        AuditConfig(include_security_names=True, include_field_names=True, include_parameters=True)
    )
    try:
        audit.record(AuditEvent(action="request", extra={"securities": ["A"], "parameters": {"k": "v"}}))
    finally:
        logging.getLogger("bloomberg_mcp.audit").handlers.clear()
    payload = json.loads(records[-1])
    assert payload["extra"]["securities"] == ["A"]
    assert payload["extra"]["parameters"] == {"k": "v"}


def test_client_request_id_hashed() -> None:
    digest = hash_client_request_id("hermes-job-42-step-3")
    assert len(digest) == 32
    assert "hermes-job-42" not in digest


def test_file_store_path_traversal_rejected(tmp_path) -> None:
    store = FileStore(str(tmp_path), maximum_total_bytes=10_000)
    info = ArtifactInfo(
        result_id="../evil",
        principal_id="hermes",
        representation="canonical-events",
        format="jsonl",
        content_type="application/x-ndjson",
        byte_count=1,
        message_count=1,
        sha256="0",
        expires_at=utc_now() + timedelta(hours=1),
        backend="file",
    )
    with pytest.raises(ValueError):
        store.put(info, b"x")


def test_file_store_quota(tmp_path) -> None:
    store = FileStore(str(tmp_path), maximum_total_bytes=10)

    def make(result_id: str, size: int) -> ArtifactInfo:
        return ArtifactInfo(
            result_id=result_id,
            principal_id="hermes",
            representation="canonical-events",
            format="jsonl",
            content_type="application/x-ndjson",
            byte_count=size,
            message_count=1,
            sha256="0",
            expires_at=utc_now() + timedelta(hours=1),
            backend="file",
        )

    assert store.put(make("res_a", 6), b"a" * 6)
    assert not store.put(make("res_b", 6), b"b" * 6)
    store.remove("res_a")
    assert store.put(make("res_b", 6), b"b" * 6)


def test_file_store_expiry(tmp_path) -> None:
    store = FileStore(str(tmp_path), maximum_total_bytes=1000)
    expired = ArtifactInfo(
        result_id="res_old",
        principal_id="hermes",
        representation="canonical-events",
        format="jsonl",
        content_type="application/x-ndjson",
        byte_count=2,
        message_count=1,
        sha256="0",
        expires_at=utc_now() - timedelta(seconds=1),
        backend="file",
    )
    store.put(expired, b"zz")
    assert store.get("res_old") is None


def test_instance_lock_single_instance() -> None:
    first = InstanceLock()
    first.acquire()
    try:
        second = InstanceLock()
        from bloomberg_mcp.instance_lock import InstanceLockHeld

        with pytest.raises(InstanceLockHeld):
            second.acquire()
    finally:
        first.release()
    # After release, acquisition succeeds again.
    third = InstanceLock()
    third.acquire()
    third.release()
