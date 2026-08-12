from datetime import datetime
from pathlib import Path

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.gui.status_model import (
    CellState,
    RunActivity,
    RunPhase,
    build_patient_status_rows,
    cell_state_for_evidence,
)


def test_run_phases_explain_interrupted_and_report_recovery_states():
    assert RunPhase.READY.label == "Ready"
    assert RunPhase.INTERRUPTED.label == "Interrupted · resume available"
    assert RunPhase.RETRY_AVAILABLE.label == "Complete · retry available"
    assert RunPhase.REPORT_PENDING.label == "Report save pending"


def test_retryable_evidence_remains_actionable():
    evidence = DatabaseEvidence("Franklin", "partial_capture", "recapture")

    assert cell_state_for_evidence(evidence) is CellState.RETRY


def test_not_found_is_distinct_from_successful_evidence():
    evidence = DatabaseEvidence("ClinVar", "not_found", "No exact GRCh37 record")

    assert cell_state_for_evidence(evidence) is CellState.NOT_FOUND


def test_activity_preserves_patient_provider_and_variant_context():
    item = RunActivity(
        occurred_at=datetime(2026, 8, 12, 20, 0),
        patient_id="SYNTHETIC01",
        database="ClinVar",
        variant_label="TP53 c.524G>A",
        action="Capturing classification",
        message="Exact GRCh37 record verified",
    )

    assert item.patient_id == "SYNTHETIC01"
    assert item.database == "ClinVar"
    assert item.variant_label == "TP53 c.524G>A"
    assert item.severity == "info"


def test_patient_rows_combine_source_and_report_state():
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=2,
        sample="SYNTHETIC01_VPM_A",
        symbol="TP53",
        hgvsc="NM_000546.6:c.524G>A",
    )

    rows = build_patient_status_rows(
        [variant],
        databases=["ClinVar", "Franklin"],
        evidence={
            "SYNTHETIC01_VPM_A|NM_000546.6:c.524G>A": [
                DatabaseEvidence("ClinVar", "found", "Pathogenic")
            ]
        },
        skipped_keys=set(),
        report_outcomes={"SYNTHETIC01": "pending"},
        active=("SYNTHETIC01", "Franklin"),
    )

    assert rows[0].patient_id == "SYNTHETIC01"
    assert rows[0].variant_count == 1
    assert rows[0].cells["ClinVar"].state is CellState.COMPLETE
    assert rows[0].cells["Franklin"].state is CellState.RUNNING
    assert rows[0].cells["Report"].state is CellState.SAVE_PENDING


def test_retryable_variant_takes_precedence_over_completed_variant():
    variants = [
        VariantRecord(
            source_file=Path("synthetic.tsv"),
            source_row=row,
            sample="SYNTHETIC01_VPM_A",
            symbol="TP53",
            hgvsc=hgvsc,
        )
        for row, hgvsc in (
            (2, "NM_000546.6:c.524G>A"),
            (3, "NM_000546.6:c.743G>A"),
        )
    ]
    evidence = {
        "SYNTHETIC01_VPM_A|NM_000546.6:c.524G>A": [
            DatabaseEvidence("Franklin", "found", "Pathogenic")
        ],
        "SYNTHETIC01_VPM_A|NM_000546.6:c.743G>A": [
            DatabaseEvidence("Franklin", "partial_capture", "Recapture")
        ],
    }

    rows = build_patient_status_rows(
        variants,
        databases=["Franklin"],
        evidence=evidence,
        skipped_keys=set(),
        report_outcomes={},
    )

    assert rows[0].cells["Franklin"].state is CellState.RETRY
    assert rows[0].cells["Report"].state is CellState.NOT_READY
