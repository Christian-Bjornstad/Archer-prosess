from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from archer_processor.core.models import DatabaseEvidence, ProcessingResult, VariantRecord
from archer_processor.reports.patient_excel import (
    PatientExcelReportWriter,
    patient_report_filename,
)

VEDLEGG_APP_DIRECTORY = "VEDLEGG_APP"


def patient_report_path(review_workbook: Path, patient_id: str) -> Path:
    """Approved destination for one patient's VPM interpretation workbook."""
    return (
        review_workbook.parent
        / VEDLEGG_APP_DIRECTORY
        / patient_report_filename(patient_id)
    )


@dataclass(frozen=True, slots=True)
class PatientReportOutcome:
    patient_id: str
    path: Path
    status: str
    message: str


class PatientReportCoordinator:
    def __init__(
        self,
        result: ProcessingResult,
        variants: Sequence[VariantRecord],
        evidence: dict[str, list[DatabaseEvidence]],
        writer: PatientExcelReportWriter | None = None,
    ) -> None:
        self.result = result
        self.variants = list(variants)
        self.evidence = {key: list(items) for key, items in evidence.items()}
        self.writer = writer or PatientExcelReportWriter()
        self.pending: set[str] = set()
        self.written: set[str] = set()

    def merge(self, incoming: dict[str, list[DatabaseEvidence]]) -> None:
        for key, items in incoming.items():
            current = {item.database: item for item in self.evidence.get(key, [])}
            current.update({item.database: item for item in items})
            self.evidence[key] = list(current.values())

    def write_patient(self, patient_id: str) -> PatientReportOutcome:
        if self.result.output_path is None:
            raise ValueError("Patient reports require a processed workbook path.")
        variants = [item for item in self.variants if item.patient_id == patient_id]
        output = patient_report_path(self.result.output_path, patient_id)
        existed = output.exists()
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            self.writer.write_patient(
                self.result, patient_id, variants, output, self.evidence
            )
        except PermissionError as exc:
            self.pending.add(patient_id)
            return PatientReportOutcome(patient_id, output, "locked", str(exc))
        except Exception as exc:
            return PatientReportOutcome(patient_id, output, "failed", str(exc))
        self.pending.discard(patient_id)
        self.written.add(patient_id)
        return PatientReportOutcome(
            patient_id,
            output,
            "updated" if existed else "created",
            "Patient report written.",
        )

    def write_patients(self, patient_ids: Sequence[str]) -> list[PatientReportOutcome]:
        unique = sorted(dict.fromkeys(patient_ids))
        return [self.write_patient(patient_id) for patient_id in unique]

    def reconcile(self) -> list[PatientReportOutcome]:
        patients = {variant.patient_id for variant in self.variants}
        required = (patients - self.written) | self.pending
        return self.write_patients(sorted(required))

    def retry_pending(self) -> list[PatientReportOutcome]:
        return self.write_patients(sorted(self.pending))
