from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Route(StrEnum):
    MATERIALIZE = "materialize"
    EXPORT = "export"


class OutcomeStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    INTERRUPTED = "interrupted"


class ProcedureStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class AcquisitionStatus(StrEnum):
    ACQUIRED = "acquired"
    PARTIAL = "partial"
    NOT_ACQUIRED = "not_acquired"


@dataclass(frozen=True)
class ActionRef:
    event_id: str
    action_id: str
    action: str
    status: str
    attempt_id: str | None
    context: dict[str, Any]


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    relative_path: str
    size: int
    sha256: str
    action_id: str
    attempt_id: str
    observation_id: str | None
    action: str
    context: dict[str, Any]


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    reason: str | None = None
    action: ActionRef | None = None
    context: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AcquisitionItem:
    acquisition_item_id: str
    target_id: str
    item_type: str
    source: dict[str, Any]
    context: dict[str, Any]


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    acquisition_item_id: str
    method: Route
    acquisition_environment: str
    procedure_status: ProcedureStatus | None
    acquisition_status: AcquisitionStatus | None
    reason: str | None
    context: dict[str, Any]
    details: dict[str, Any]
    started_at: str | None = None
    ended_at: str | None = None
    applied_rules: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentificationRecord:
    target_id: str
    acquisition_environment: str
    identification_status: str
    started_at: str | None
    ended_at: str | None
    traversal_complete: bool
    identified_item_count: int
    reason: str | None
    applied_rules: dict[str, str]
    completion_condition: str | None = None


@dataclass(frozen=True)
class AcquisitionOutcome:
    outcome_id: str
    attempt_id: str
    acquisition_status: AcquisitionStatus
    reason: str
    action_id: str | None
    observation_id: str | None
    context: dict[str, Any]
    details: dict[str, Any]


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    attempt_id: str
    action_id: str
    screen_artifact_id: str
    hierarchy_artifact_id: str
    source_snapshot_id: str | None = None


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    outcome: Outcome
    items: tuple[AcquisitionItem, ...]
    attempts: tuple[AttemptRecord, ...]
    outcomes: tuple[AcquisitionOutcome, ...]
    observations: tuple[ObservationRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]
