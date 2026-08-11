from pathlib import Path

from archer_processor.core.models import ProcessingResult, VariantRecord
from archer_processor.reports.patient_report_coordinator import (
    PatientReportCoordinator,
)


def _result(tmp_path: Path):
    variant = VariantRecord(
        source_file=tmp_path / "synthetic.tsv",
        source_row=2,
        sample="SYNTHETIC01_VPM_A",
        symbol="TP53",
        hgvsc="NM_000546.6:c.524G>A",
    )
    result = ProcessingResult(
        input_path=variant.source_file,
        output_path=tmp_path / "review.xlsx",
        run_date="2026-08-11",
        variants=[variant],
        rules_applied=[],
    )
    return result, variant


def test_patient_report_written_beside_processed_workbook(tmp_path):
    result, variant = _result(tmp_path)
    coordinator = PatientReportCoordinator(result, [variant], {})

    outcome = coordinator.write_patient("SYNTHETIC01")

    assert outcome.status == "written"
    assert outcome.path == tmp_path / "SYNTHETIC01_VPM_Tolkning.xlsx"
    assert outcome.path.exists()


def test_locked_patient_report_is_pending_and_reconciles(tmp_path, monkeypatch):
    result, variant = _result(tmp_path)
    coordinator = PatientReportCoordinator(result, [variant], {})
    original = coordinator.writer.write_patient
    calls = 0

    def locked_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(13, "locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator.writer, "write_patient", locked_once)
    first = coordinator.write_patient("SYNTHETIC01")
    final = coordinator.reconcile()

    assert first.status == "pending"
    assert final[0].status == "written"
    assert not coordinator.pending


def test_reconcile_writes_every_selected_patient_once(tmp_path):
    result, variant = _result(tmp_path)
    second = VariantRecord(
        source_file=variant.source_file,
        source_row=3,
        sample="SYNTHETIC02_VPM_A",
        symbol="KRAS",
        hgvsc="NM_004985.5:c.35G>A",
    )
    result.variants.append(second)
    coordinator = PatientReportCoordinator(result, [variant, second], {})

    coordinator.write_patient("SYNTHETIC01")
    outcomes = coordinator.reconcile()

    assert [item.patient_id for item in outcomes] == ["SYNTHETIC02"]
    assert (tmp_path / "SYNTHETIC02_VPM_Tolkning.xlsx").exists()
