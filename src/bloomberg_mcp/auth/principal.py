"""Server-owned principal model (SPEC §1.7).

Tokens map to server-owned principals and scope sets; application handles are
never treated as authentication.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    principal_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    admin: bool = False

    def has_scope(self, scope: str) -> bool:
        return self.admin or scope in self.scopes
