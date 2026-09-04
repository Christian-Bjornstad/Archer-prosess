from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Sequence

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.evidence_audit import is_completed_evidence


class RunPhase(str, Enum):
    READY = "ready"
    LOADING = "loading"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    INTERRUPTED = "interrupted"
    RETRY_AVAILABLE = "retry_available"
    COMPLETE = "complete"
    REPORT_PENDING = "report_pending"

    @property
    def label(self) -> str:
        return {
            RunPhase.READY: "Ready",
            RunPhase.LOADING: "Loading workbook",
            RunPhase.RUNNING: "Running",
            RunPhase.PAUSING: "Pausing",
            RunPhase.PAUSED: "Paused",
            RunPhase.STOPPING: "Stopping",
            RunPhase.INTERRUPTED: "Interrupted · resume available",
            RunPhase.RETRY_AVAILABLE: "Complete · retry available",
            RunPhase.COMPLETE: "Complete",
            RunPhase.REPORT_PENDING: "Report save pending",
        }[self]


class CellState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"
    RETRY = "retry"
    STOPPED = "stopped"
    SKIPPED = "skipped"
    REPORT_SAVED = "report_saved"
    SAVE_PENDING = "save_pending"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class RunActivity:
    occurred_at: datetime
    patient_id: str = ""
    database: str = ""
    variant_label: str = ""
    action: str = ""
    message: str = ""
    severity: str = "info"


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    phase: RunPhase = RunPhase.READY
    current_patient: int = 0
    patient_total: int = 0
    patient_id: str = ""
    database: str = ""
    variant_label: str = ""
    action: str = ""
    completed_sources: int = 0
    source_total: int = 0
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StatusCell:
    state: CellState
    label: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PatientStatusRow:
    patient_id: str
    variant_count: int
    cells: dict[str, StatusCell]


STATE_LABELS = {
    CellState.QUEUED: "Queued",
    CellState.RUNNING: "Running",
    CellState.COMPLETE: "Complete",
    CellState.NOT_FOUND: "Not found",
    CellState.RETRY: "Retry",
    CellState.STOPPED: "Stopped",
    CellState.SKIPPED: "Skipped",
    CellState.REPORT_SAVED: "Report saved",
    CellState.SAVE_PENDING: "Save pending",
    CellState.NOT_READY: "Not ready",
}

STATE_PRIORITY = {
    CellState.RUNNING: 6,
    CellState.RETRY: 5,
    CellState.QUEUED: 4,
    CellState.NOT_FOUND: 3,
    CellState.COMPLETE: 2,
    CellState.SKIPPED: 1,
}


def cell_state_for_evidence(evidence: DatabaseEvidence) -> CellState:
    if evidence.status.strip().casefold() == "not_found":
        return CellState.NOT_FOUND
    if is_completed_evidence(evidence):
        return CellState.COMPLETE
    return CellState.RETRY


def build_patient_status_rows(
    variants: Sequence[VariantRecord],
    *,
    databases: Sequence[str],
    evidence: dict[str, list[DatabaseEvidence]],
    skipped_keys: set[str],
    report_outcomes: dict[str, str],
    active: tuple[str, str] | None = None,
) -> list[PatientStatusRow]:
    grouped: dict[str, list[VariantRecord]] = {}
    for variant in variants:
        grouped.setdefault(variant.patient_id, []).append(variant)

    rows: list[PatientStatusRow] = []
    for patient_id, patient_variants in grouped.items():
        cells: dict[str, StatusCell] = {}
        for database in databases:
            states: list[CellState] = []
            for variant in patient_variants:
                key = f"{variant.sample}|{variant.hgvsc}"
                if active == (patient_id, database):
                    states.append(CellState.RUNNING)
                elif key in skipped_keys:
                    states.append(CellState.SKIPPED)
                else:
                    item = next(
                        (
                            candidate
                            for candidate in evidence.get(key, [])
                            if candidate.database == database
                        ),
                        None,
                    )
                    states.append(
                        CellState.QUEUED
                        if item is None
                        else cell_state_for_evidence(item)
                    )
            state = max(states, key=STATE_PRIORITY.get)
            cells[database] = StatusCell(state, STATE_LABELS[state])

        report_state = {
            "created": CellState.REPORT_SAVED,
            "updated": CellState.REPORT_SAVED,
            "locked": CellState.SAVE_PENDING,
            "failed": CellState.RETRY,
        }.get(report_outcomes.get(patient_id, ""), CellState.NOT_READY)
        cells["Report"] = StatusCell(report_state, STATE_LABELS[report_state])
        rows.append(PatientStatusRow(patient_id, len(patient_variants), cells))
    return rows
