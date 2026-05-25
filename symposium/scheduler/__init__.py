"""Scheduler — the orchestrator_runtime (§4).

Owns: session lifecycle (§4.1), round structure (§4.2), context_packet
derivation (§4.3), verdict handling (§4.4), direct_request routing
(§4.5), deferred queue (§4.6), hard caps (§4.7), synthesis attempt
(§4.8), agent-failure policy (§4.9), and the §4.11 pseudocode loop.

Public entry point: `run_session(config, providers_by_id) -> Artifact`.
"""

from symposium.scheduler.loop import Session, run_session

__all__ = ["Session", "run_session"]
