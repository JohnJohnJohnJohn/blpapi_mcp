"""Compositional readiness model (SPEC §4.9).

A disconnected Bloomberg session never makes the HTTP process unavailable:
liveness stays UP while component readiness degrades.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bloomberg_mcp.blp.backend import BloombergBackend
    from bloomberg_mcp.policy.quota import QuotaEngine
    from bloomberg_mcp.storage.result_store import ResultStore


@dataclass(frozen=True)
class HealthInputs:
    backend: BloombergBackend
    quota: QuotaEngine
    result_store: ResultStore


class HealthService:
    def __init__(self, inputs: HealthInputs) -> None:
        self._inputs = inputs

    def liveness(self) -> dict[str, str]:
        # Unauthenticated endpoint: boolean liveness only (SPEC §3.1).
        return {"status": "UP"}

    def readiness(self) -> dict[str, Any]:
        backend = self._inputs.backend
        session_state = backend.session_state
        connected = session_state.value == "CONNECTED"
        services = backend.service_states()
        admission = "ACCEPTING" if connected else "REJECTING"
        return {
            "process": "UP",
            "mcp_transport": "READY",
            "authentication": "READY",
            "bloomberg_session": session_state.value if connected else "DISCONNECTED",
            "session_generation": backend.session_generation,
            "required_services": {
                name: ("OPEN" if opened else "CLOSED") for name, opened in services.items()
            },
            "request_admission": admission,
            "subscription_admission": admission,
            "result_store": "READY" if self._inputs.result_store.is_ready() else "DEGRADED",
            "entitlement_circuit": "OPEN" if self._inputs.quota.entitlement_circuit_open else "CLOSED",
        }
