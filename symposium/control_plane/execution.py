"""Bind a Symposium 2.x room to one immutable v1 deliberation run.

The control plane decides which room members may speak and which private
onboarding context each receives.  The existing v1 runtime still owns the
conversation, transcript, digest, and artifact.  This host-layer manager only
prepares a valid ``Config`` and runs it on a background thread so the local
browser server remains responsive.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional

from symposium.control_plane.models import (
    AgentRecord,
    ControlPlaneState,
    RoomMembership,
    RoomRecord,
)
from symposium.control_plane.service import ControlPlane, ControlPlaneError
from symposium.models import (
    AgentConfig,
    Artifact,
    BudgetConfig,
    Config,
    Persona,
    RuntimeConfig,
    SelectorConfig,
    now_utc_iso,
)
from symposium.personas import COORDINATOR, persona_by_id
from symposium.scheduler import run_session

ExecutionStatus = Literal["preparing", "running", "completed", "failed"]
ProviderRouter = Callable[[Config], tuple[Config, Dict[str, Any]]]
SessionRunner = Callable[..., Artifact]

_MAX_PROBLEM_LENGTH = 20_000
_FAILED_TERMINATIONS = {"provider_unrecoverable", "schema_error", "timeout"}


@dataclass(slots=True)
class RoomExecutionJob:
    """In-memory status for a run owned by the current viewer process."""

    id: str
    room_id: str
    room_name: str
    session_id: str
    problem: str
    participant_ids: list[str]
    status: ExecutionStatus
    created_at: str
    updated_at: str
    outcome: Optional[str] = None
    termination_reason: Optional[str] = None
    error: Optional[str] = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "room_name": self.room_name,
            "run_name": self.session_id,
            "problem": self.problem,
            "participant_ids": list(self.participant_ids),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "outcome": self.outcome,
            "termination_reason": self.termination_reason,
            "error": self.error,
        }


def _room_from_state(state: ControlPlaneState, reference: Optional[str]) -> RoomRecord:
    wanted = reference or state.workspace.active_room_id
    if wanted in state.rooms:
        room = state.rooms[wanted]
    else:
        matches = [
            candidate
            for candidate in state.rooms.values()
            if candidate.name.casefold() == wanted.casefold()
        ]
        if len(matches) != 1:
            raise ControlPlaneError(f"unknown room {wanted!r}")
        room = matches[0]
    if room.status == "archived":
        raise ControlPlaneError(f"room {room.id!r} is archived")
    return room


def _persona_for_membership(
    agent: AgentRecord,
    membership: RoomMembership,
    room: RoomRecord,
) -> Persona:
    """Resolve a built-in persona or construct a strict custom one."""
    try:
        persona = persona_by_id(agent.id)
    except KeyError:
        capabilities = "; ".join(agent.capabilities)
        persona = Persona(
            persona_class="horizontal",
            id=agent.id,
            reasoning_scope=capabilities or f"specialist role: {agent.display_name}",
            reasoning_style=agent.instructions,
            behavioral_constraints=[
                agent.instructions,
                "state facts, uncertainty, risks, blockers, and requested decisions clearly",
            ],
            failure_modes=[
                "invent facts beyond the supplied room context",
                "hide uncertainty or exceed the assigned remit without saying so",
            ],
            output_requirements=["answer the user's request from the assigned role"],
        )

    disclosed = [
        *persona.behavioral_constraints,
        f"Room purpose: {room.purpose}",
    ]
    if membership.onboarding_context:
        disclosed.append(f"Room onboarding context: {membership.onboarding_context}")
    return persona.model_copy(update={"behavioral_constraints": disclosed})


def build_room_config(
    control: ControlPlane,
    problem: str,
    *,
    session_id: str,
    room: Optional[str] = None,
    max_rounds: int = 2,
    max_wallclock_seconds: int = 1800,
) -> tuple[Config, RoomRecord, list[str]]:
    """Project the speaking members of one room into a valid v1 config."""
    question = problem.strip()
    if not question:
        raise ControlPlaneError("scrivi una domanda per la stanza")
    if len(question) > _MAX_PROBLEM_LENGTH:
        raise ControlPlaneError(
            f"la domanda supera il limite di {_MAX_PROBLEM_LENGTH} caratteri"
        )

    state = control.ensure_initialized()
    room_record = _room_from_state(state, room)
    speaking_members = [
        membership
        for membership in state.memberships.values()
        if membership.room_id == room_record.id
        and membership.presence != "offline"
        and membership.agent_id != "coordinator"
        and membership.role != "observer"
        and state.agents[membership.agent_id].status == "active"
    ]
    if not speaking_members:
        raise ControlPlaneError(
            "invita almeno un agente che possa parlare prima di avviare la discussione"
        )

    agents = []
    for membership in speaking_members:
        agent = state.agents[membership.agent_id]
        agents.append(AgentConfig(
            id=agent.id,
            persona_ref=_persona_for_membership(agent, membership, room_record),
            provider="claude-cli",
            model="opus",
        ))
    participant_ids = [agent.id for agent in agents]
    config = Config(
        schema_version="1.0.0",
        session_id=session_id,
        originator="user:browser",
        problem_statement=question,
        selector=SelectorConfig(
            strategy="fixed",
            default_deliberation_panel=participant_ids,
            coordinator_agent="coordinator",
        ),
        agents=agents,
        coordinator=AgentConfig(
            id="coordinator",
            persona_ref=COORDINATOR,
            provider="claude-cli",
            model="opus",
        ),
        budget=BudgetConfig(
            # CLI-backed sessions reuse local subscriptions. These generous
            # token/cost values are telemetry canaries; wall time is the real
            # browser-run bound.
            max_total_tokens=100_000_000,
            max_total_cost_usd=1000.0,
            max_rounds=max_rounds,
            max_wallclock_seconds=max_wallclock_seconds,
        ),
        runtime=RuntimeConfig(synthesize_on_terminate=True),
    )
    return config, room_record, participant_ids


def _default_provider_router(config: Config) -> tuple[Config, Dict[str, Any]]:
    from symposium.integrations.cli_routing import route_cli_providers

    return route_cli_providers(config)


class RoomExecutionManager:
    """Start and observe one local room deliberation at a time."""

    def __init__(
        self,
        control: ControlPlane,
        runs_root: Path,
        *,
        provider_router: ProviderRouter = _default_provider_router,
        runner: SessionRunner = run_session,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        clock: Callable[[], str] = now_utc_iso,
    ) -> None:
        self.control = control
        self.runs_root = Path(runs_root)
        self.provider_router = provider_router
        self.runner = runner
        self.id_factory = id_factory
        self.clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, RoomExecutionJob] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start(self, problem: str, *, room: Optional[str] = None) -> RoomExecutionJob:
        token = self.id_factory()
        session_id = f"room-session-{token[:16]}"
        if (self.runs_root / session_id).exists():
            raise ControlPlaneError(
                f"run {session_id!r} already exists; no existing artifact was changed"
            )
        config, room_record, participant_ids = build_room_config(
            self.control,
            problem,
            session_id=session_id,
            room=room,
        )

        with self._lock:
            job_id = f"job-{token[:16]}"
            if job_id in self._jobs:
                raise ControlPlaneError(f"execution job {job_id!r} already exists")
            if any(
                job.status in {"preparing", "running"}
                for job in self._jobs.values()
            ):
                raise ControlPlaneError(
                    "una discussione è già in corso; attendi che termini prima di avviarne un'altra"
                )
            # Provider availability is resolved before the HTTP response, so
            # a missing local CLI produces an immediate actionable error.
            routed_config, providers = self.provider_router(config)
            now = self.clock()
            job = RoomExecutionJob(
                id=job_id,
                room_id=room_record.id,
                room_name=room_record.name,
                session_id=routed_config.session_id,
                problem=routed_config.problem_statement,
                participant_ids=participant_ids,
                status="preparing",
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.id] = job

        try:
            self.control.request_briefing(
                routed_config.problem_statement,
                session_id=job.session_id,
                room=room_record.id,
                participant_ids=participant_ids,
            )
        except Exception:
            with self._lock:
                self._jobs.pop(job.id, None)
            raise

        worker = threading.Thread(
            target=self._run,
            args=(job.id, routed_config, providers),
            name=f"symposium-{job.id}",
            daemon=True,
        )
        with self._lock:
            self._threads[job.id] = worker
        worker.start()
        return self.get(job.id)

    def _run(self, job_id: str, config: Config, providers: Dict[str, Any]) -> None:
        self._update(job_id, status="running")
        try:
            artifact = self.runner(config, providers, runs_root=str(self.runs_root))
        except Exception as exc:  # noqa: BLE001 — surfaced to the local UI
            message = f"{type(exc).__name__}: {exc}"[:2000]
            job = self._update(job_id, status="failed", error=message)
            try:
                self.control.finish_briefing(
                    session_id=job.session_id,
                    room=job.room_id,
                    error=message,
                )
            except Exception:  # noqa: BLE001 — never hide the execution failure
                pass
            return

        termination_reason = None
        execution_error = None
        if artifact.outcome.kind == "termination":
            termination = artifact.outcome.termination_artifact
            termination_reason = termination.reason
            if termination_reason in _FAILED_TERMINATIONS:
                failure = termination.last_provider_failure
                if failure is not None:
                    execution_error = (
                        f"{failure.agent_id} non ha potuto completare l'intervento "
                        f"tramite {failure.provider}: {failure.message}"
                    )[:2000]
                else:
                    execution_error = (
                        f"La discussione si è interrotta: {termination_reason}"
                    )
        job = self._update(
            job_id,
            status="failed" if execution_error else "completed",
            outcome=artifact.outcome.kind,
            termination_reason=termination_reason,
            error=execution_error,
        )
        try:
            self.control.finish_briefing(
                session_id=job.session_id,
                room=job.room_id,
                outcome=artifact.outcome.kind,
                termination_reason=termination_reason,
                error=execution_error,
            )
        except Exception:  # noqa: BLE001 — run artifacts are already complete
            pass

    def _update(self, job_id: str, **changes: Any) -> RoomExecutionJob:
        with self._lock:
            job = self._jobs[job_id]
            for field, value in changes.items():
                setattr(job, field, value)
            job.updated_at = self.clock()
            return RoomExecutionJob(**{
                field: getattr(job, field)
                for field in RoomExecutionJob.__dataclass_fields__
            })

    def get(self, job_id: str) -> RoomExecutionJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ControlPlaneError(f"unknown execution job {job_id!r}")
            return RoomExecutionJob(**{
                field: getattr(job, field)
                for field in RoomExecutionJob.__dataclass_fields__
            })

    def public_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())[-20:]
            return [job.public_payload() for job in reversed(jobs)]

    def has_active_job(self) -> bool:
        with self._lock:
            return any(
                job.status in {"preparing", "running"}
                for job in self._jobs.values()
            )

    def wait(self, job_id: str, timeout: Optional[float] = None) -> RoomExecutionJob:
        """Test/library convenience: wait for one worker without blocking HTTP."""
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout=timeout)
        return self.get(job_id)
