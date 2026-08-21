"""Static bearer token verification (SPEC §1.7).

- Tokens come from the ``Authorization: Bearer`` header only; query strings,
  paths, cookies and bodies are never consulted (enforced by the middleware).
- Comparison uses constant-time equality over SHA-256 digests.
- A bounded rotation overlap accepts the previous token alongside the current
  one.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.config import AuthConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.policy.models import PolicyConfig

logger = logging.getLogger(__name__)

MINIMUM_TOKEN_BYTES = 32  # 256 bits of entropy (SPEC §1.7)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _read_windows_credential(target: str) -> str | None:
    """Read a generic credential from the Windows Credential Manager."""
    if sys.platform != "win32":
        raise GatewayError(
            ErrorCode.INVALID_ARGUMENT,
            "windows_credential_manager token source requires Windows",
        )
    import ctypes
    from ctypes import wintypes

    class _Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", wintypes.LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.windll.advapi32
    out = ctypes.POINTER(_Credential)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(out)):
        return None
    try:
        blob = ctypes.string_at(out.contents.CredentialBlob, out.contents.CredentialBlobSize)
        return blob.decode("utf-8").strip()
    finally:
        advapi32.CredFree(out)


class TokenVerifier:
    """Maps bearer tokens to server-owned principals."""

    def __init__(self, auth: AuthConfig, policy: PolicyConfig) -> None:
        self._auth = auth
        self._policy = policy
        # sha256(token) -> principal_id ; current and previous tokens both map.
        self._token_digests: dict[bytes, str] = {}
        # The previous token is valid only until its bounded overlap expiry.
        self._previous_digest: bytes | None = None
        self._previous_expires_at: datetime | None = None
        self._load_tokens()

    def _load_tokens(self) -> None:
        current = self._read_token("BLOOMBERG_MCP_BEARER_TOKEN")
        previous = self._read_previous_token()
        if not current:
            raise GatewayError(
                ErrorCode.AUTH_REQUIRED,
                "no bearer token configured (set BLOOMBERG_MCP_BEARER_TOKEN or the configured token source)",
            )
        if len(current.encode("utf-8")) < MINIMUM_TOKEN_BYTES:
            raise GatewayError(
                ErrorCode.INVALID_ARGUMENT,
                "bearer token must contain at least 256 bits of entropy",
            )
        principal_ids = list(self._policy.principals)
        if not principal_ids:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "policy defines no principals")
        if self._auth.principal_id:
            # Explicit binding (finding M1): the token resolves to one named
            # principal; a nonexistent principal id is a startup error.
            if self._auth.principal_id not in self._policy.principals:
                raise GatewayError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"auth.principal_id {self._auth.principal_id!r} is not a defined policy principal",
                )
            principal_id = self._auth.principal_id
        else:
            # Single-principal invariant (finding M1): with more than one
            # configured principal and no explicit binding the mapping is
            # ambiguous, so startup refuses rather than silently collapsing
            # every token onto the first principal.
            if len(principal_ids) > 1:
                raise GatewayError(
                    ErrorCode.INVALID_ARGUMENT,
                    "policy defines multiple principals but auth.principal_id is unset; "
                    "configure the explicit token->principal mapping",
                )
            principal_id = principal_ids[0]
        self._token_digests[_digest(current)] = principal_id
        if previous:
            self._previous_digest = _digest(previous)
            self._previous_expires_at = datetime.now(UTC) + timedelta(seconds=self._auth.token_overlap_seconds)
            self._token_digests[self._previous_digest] = principal_id
        logger.info("loaded %d bearer token(s)", len(self._token_digests))

    def _read_previous_token(self) -> str | None:
        """Read the previous token from a genuinely separate source (finding M3)."""
        if self._auth.token_source == "file" and self._auth.previous_token_file:  # noqa: S105 - config field name
            path = self._auth.previous_token_file
            try:
                with open(path, encoding="utf-8") as handle:
                    return handle.read().strip() or None
            except OSError as exc:
                raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"cannot read previous token file: {exc}") from exc
        # env (and credential-manager) sources already use a distinct name.
        return self._read_token("BLOOMBERG_MCP_BEARER_TOKEN_PREVIOUS")

    def _read_token(self, env_name: str) -> str | None:
        source = self._auth.token_source  # noqa: S105 - config field name, not a credential
        if source == "env":
            return os.environ.get(env_name) or None
        if source == "file":
            path = self._auth.token_file
            if not path or not os.path.isfile(path):
                return os.environ.get(env_name) or None
            try:
                with open(path, encoding="utf-8") as handle:
                    return handle.read().strip() or None
            except OSError as exc:
                raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"cannot read token file: {exc}") from exc
        if source == "windows_credential_manager":
            value = _read_windows_credential(self._auth.credential_target)
            return value or os.environ.get(env_name) or None
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"unknown token_source {source!r}")

    def verify(self, presented: str) -> Principal:
        """Return the principal for a presented token.

        Constant-time: the candidate is hashed once and compared with
        ``hmac.compare_digest`` against every registered digest.
        """
        candidate = _digest(presented)
        if (
            self._previous_digest is not None
            and hmac.compare_digest(candidate, self._previous_digest)
            and self._previous_expires_at is not None
            and datetime.now(UTC) >= self._previous_expires_at
        ):
            # Rotation overlap expired (finding M2): the previous token no
            # longer authenticates.
            self._token_digests.pop(self._previous_digest, None)
            raise GatewayError(ErrorCode.AUTH_INVALID, "Invalid bearer token.")
        matched: str | None = None
        for digest, principal_id in self._token_digests.items():
            if hmac.compare_digest(candidate, digest):
                matched = principal_id
        if matched is None:
            raise GatewayError(ErrorCode.AUTH_INVALID, "Invalid bearer token.")
        principal = self._policy.principals.get(matched)
        if principal is None:
            raise GatewayError(ErrorCode.AUTH_INVALID, "Invalid bearer token.")
        return principal
