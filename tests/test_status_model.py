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


def test_manual_review_is_visible_without_automatic_retry():
    evidence = DatabaseEvidence(
        "OncoKB", "manual_review", "Canonical transcript differs"
    )

    assert cell_state_for_evidence(evidence) is CellState.MANUAL_REVIEW


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
        report_outcomes={"SYNTHETIC01": "locked"},
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


def test_active_retry_only_marks_the_attempted_variant_running():
    variants = [
        VariantRecord(Path("synthetic.tsv"), row, "SYNTHETIC01", "TP53", change)
        for row, change in ((2, "c.524G>A"), (3, "c.743G>A"))
    ]
    rows = build_patient_status_rows(
        variants, databases=["ClinVar"],
        evidence={"SYNTHETIC01|c.524G>A": [DatabaseEvidence("ClinVar", "timeout", "slow")]},
        skipped_keys=set(), report_outcomes={},
        active={("SYNTHETIC01", "ClinVar")},
        active_keys={("SYNTHETIC01|c.524G>A", "ClinVar")},
    )
    cell = rows[0].cells["ClinVar"]
    assert cell.label == "Running (1/2)"
    assert "Queued: 1" in cell.detail


def test_mixed_patient_status_explains_found_and_missing_counts():
    variants = [
        VariantRecord(Path("synthetic.tsv"), row, "SYNTHETIC01", "TP53", change)
        for row, change in ((2, "c.524G>A"), (3, "c.743G>A"))
    ]
    rows = build_patient_status_rows(
        variants, databases=["ClinVar"],
        evidence={
            "SYNTHETIC01|c.524G>A": [DatabaseEvidence("ClinVar", "found", "matched")],
            "SYNTHETIC01|c.743G>A": [DatabaseEvidence("ClinVar", "not_found", "no exact result")],
        },
        skipped_keys=set(), report_outcomes={},
    )
    cell = rows[0].cells["ClinVar"]
    assert cell.label == "Not found (1/2)"
    assert "Complete: 1" in cell.detail
    assert "no exact result" in cell.detail
