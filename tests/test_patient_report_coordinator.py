from pathlib import Path

from archer_processor.core.models import ProcessingResult, VariantRecord
from archer_processor.reports.patient_report_coordinator import (
    PatientReportCoordinator,
    patient_report_path,
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


def test_patient_report_path_uses_approved_directory_and_name(tmp_path):
    review = tmp_path / "run_VPM_review.xlsx"

    assert patient_report_path(review, "26OUM12345") == (
        tmp_path / "VEDLEGG_APP" / "26OUM12345_VPM_Tolkning_APP.xlsx"
    )


def test_patient_report_path_sanitises_unsafe_characters(tmp_path):
    review = tmp_path / "run_VPM_review.xlsx"

    assert patient_report_path(review, "26 OUM/12345") == (
        tmp_path / "VEDLEGG_APP" / "26_OUM_12345_VPM_Tolkning_APP.xlsx"
    )


def test_new_patient_report_is_created_in_vedlegg_app(tmp_path):
    result, variant = _result(tmp_path)
    coordinator = PatientReportCoordinator(result, [variant], {})

    outcome = coordinator.write_patient("SYNTHETIC01")

    assert outcome.status == "created"
    assert outcome.path == (
        tmp_path / "VEDLEGG_APP" / "SYNTHETIC01_VPM_Tolkning_APP.xlsx"
    )
    assert outcome.path.exists()


def test_existing_patient_report_is_updated_atomically(tmp_path):
    result, variant = _result(tmp_path)
    coordinator = PatientReportCoordinator(result, [variant], {})
    first = coordinator.write_patient("SYNTHETIC01")
    assert first.status == "created"

    second = coordinator.write_patient("SYNTHETIC01")

    assert second.status == "updated"
    assert second.path == first.path


def test_failed_write_keeps_existing_destination_and_leaves_no_temp_files(tmp_path):
    result, variant = _result(tmp_path)
    coordinator = PatientReportCoordinator(result, [variant], {})
    assert coordinator.write_patient("SYNTHETIC01").status == "created"
    destination = patient_report_path(result.output_path, "SYNTHETIC01")
    before = destination.read_bytes()

    def broken_write(*args, **kwargs):
        raise RuntimeError("writer exploded")

    coordinator.writer.write_patient = broken_write  # type: ignore[method-assign]

    outcome = coordinator.write_patient("SYNTHETIC01")

    assert outcome.status == "failed"
    assert destination.read_bytes() == before
    assert [entry for entry in destination.parent.iterdir() if entry != destination] == []


def test_locked_patient_report_is_skipped_without_aborting_others(tmp_path):
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
    locked_target = patient_report_path(result.output_path, "SYNTHETIC01")
    locked_target.parent.mkdir(parents=True, exist_ok=True)
    locked_target.write_bytes(b"locked workbook bytes")
    original_write = coordinator.writer.write_patient

    def locked_write(result_, patient_id, variants, output, evidence):
        if patient_id == "SYNTHETIC01":
            raise PermissionError(13, "locked")
        return original_write(result_, patient_id, variants, output, evidence)

    coordinator.writer.write_patient = locked_write  # type: ignore[method-assign]

    outcomes = coordinator.write_patients(["SYNTHETIC01", "SYNTHETIC02"])

    assert [outcome.status for outcome in outcomes] == ["locked", "created"]
    assert locked_target.read_bytes() == b"locked workbook bytes"
    assert patient_report_path(result.output_path, "SYNTHETIC02").exists()


def test_reconcile_writes_every_patient_once(tmp_path):
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
    assert (tmp_path / "VEDLEGG_APP" / "SYNTHETIC02_VPM_Tolkning_APP.xlsx").exists()


def test_write_patients_supports_explicit_selection_and_deduplicates(tmp_path):
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

    outcomes = coordinator.write_patients(["SYNTHETIC02", "SYNTHETIC02", "SYNTHETIC01"])

    assert [item.patient_id for item in outcomes] == ["SYNTHETIC01", "SYNTHETIC02"]


def test_retry_locked_reports_ignores_written_patients(tmp_path, monkeypatch):
    result, variant = _result(tmp_path)
    coordinator = PatientReportCoordinator(result, [variant], {})
    coordinator.pending = {"SYNTHETIC01"}
    calls = []
    original = coordinator.write_patient

    def record(patient_id):
        calls.append(patient_id)
        return original(patient_id)

    monkeypatch.setattr(coordinator, "write_patient", record)

    outcomes = coordinator.retry_pending()

    assert calls == ["SYNTHETIC01"]
    assert outcomes[0].status in {"created", "updated"}
