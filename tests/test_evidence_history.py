from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from threading import Barrier

from PIL import Image, ImageDraw
import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.evidence_history import (
    EvidenceHistoryError,
    EvidenceHistoryStore,
    current_attempt,
)


CAPTURED = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 15, 10, 15, tzinfo=timezone.utc)


def variant(**changes):
    return replace(VariantRecord(
        source_file=Path("private-patient-file.tsv"), source_row=4,
        sample="PRIVATE_SAMPLE", symbol="TESTGENE", hgvsc="NM_999999.1:c.12A>G",
        genomic_location="chr7:12345", ref_allele="A", alt_allele="G",
    ), **changes)


def capture(path, text="Clinical evidence"):
    image = Image.new("RGB", (400, 150), "white")
    ImageDraw.Draw(image).text((20, 25), text, fill="black")
    image.save(path)
    return path


def found(tmp_path, database="ClinVar", **raw):
    screenshot = capture(tmp_path / f"{database}.png")
    return DatabaseEvidence(
        database, "found", "classification=Pathogenic", accession="VCV000123",
        clinical_significance="Pathogenic", url="https://www.ncbi.nlm.nih.gov/clinvar/variation/123/",
        raw={
            "captured_at": CAPTURED.isoformat(), "assembly_verified": "GRCh37",
            "identity_verification": {"accepted": True, "basis": "exact_transcript_grch37"},
            "screenshots": [{"label": "Classification summary", "path": str(screenshot)}],
            **raw,
        },
    )


def test_backup_survives_deleted_original_and_cross_sample_with_original_timestamp(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    evidence = found(tmp_path)
    original = asdict(evidence)
    assert store.record(variant(), evidence)
    Path(evidence.raw["screenshots"][0]["path"]).unlink()
    failed = DatabaseEvidence("ClinVar", "timeout", "page did not finish", raw={"failure_stage": "rendering"})
    restored = store.fallback(
        variant(sample="OTHER_SAMPLE", source_file=Path("other.tsv"), source_row=99),
        failed, tmp_path / "new-output",
    )

    assert asdict(evidence) == original
    assert restored.status == "historical"
    assert restored.database == "ClinVar"
    assert restored.clinical_significance == "Pathogenic"
    metadata = restored.raw["historical_result"]
    assert metadata["captured_at"] == CAPTURED.isoformat()
    assert metadata["used_at"] == NOW.isoformat()
    assert metadata["current_attempt"] == asdict(failed)
    assert current_attempt(restored) == failed
    copied = Path(restored.raw["screenshots"][0]["path"])
    assert copied.parent == tmp_path / "new-output"
    assert copied.exists()


@pytest.mark.parametrize("change", [
    {"symbol": "OTHERGENE"}, {"genomic_location": "chr7:12346"},
    {"genomic_location": "chr8:12345"}, {"ref_allele": "T"}, {"alt_allele": "C"},
])
def test_different_variant_never_reuses_result(tmp_path, change):
    store = EvidenceHistoryStore(tmp_path / "history")
    assert store.record(variant(), found(tmp_path))
    failed = DatabaseEvidence("ClinVar", "error", "failure")
    assert store.fallback(variant(**change), failed, tmp_path / "output") is failed


@pytest.mark.parametrize("change", [
    {"symbol": ""}, {"genomic_location": ""}, {"genomic_location": "chr7:0"},
    {"genomic_location": "chr99:12345"}, {"ref_allele": ""},
    {"alt_allele": "N"}, {"alt_allele": "A"}, {"ref_allele": "-"},
])
def test_incomplete_or_non_sequence_identity_is_not_recorded(tmp_path, change):
    assert not EvidenceHistoryStore(tmp_path / "history").record(variant(**change), found(tmp_path))


def test_normalization_and_assembly_are_explicit(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history")
    assert store.record(variant(), found(tmp_path))
    failed = DatabaseEvidence("ClinVar", "not_found")
    normalized = variant(symbol=" testgene ", genomic_location="7:12345", ref_allele=" a ", alt_allele=" g ")
    assert store.fallback(normalized, failed, tmp_path / "out").status == "historical"
    assert store.fallback(variant(), failed, tmp_path / "out", assembly="GRCh38") is failed
    assert store.fallback(variant(), replace(failed, database="Franklin"), tmp_path / "out").status == "not_found"


@pytest.mark.parametrize("status", ["found", "manual_review", "manual", "invalid_query", "not_applicable", "skipped", "verification_required"])
def test_backup_does_not_override_success_or_deliberate_review(tmp_path, status):
    store = EvidenceHistoryStore(tmp_path / "history")
    assert store.record(variant(), found(tmp_path))
    current = DatabaseEvidence("ClinVar", status)
    assert store.fallback(variant(), current, tmp_path / "out") is current


@pytest.mark.parametrize("status", [
    "not_found", "error", "timeout", "transient", "session_lost", "login_required",
    "unauthorized", "layout_changed", "rate_limited", "quota_exhausted", "token_required",
    "deferred", "identity_mismatch", "partial_capture",
])
def test_backup_covers_failed_current_attempts(tmp_path, status):
    store = EvidenceHistoryStore(tmp_path / "history")
    assert store.record(variant(), found(tmp_path))
    assert store.fallback(variant(), DatabaseEvidence("ClinVar", status), tmp_path / "out").status == "historical"


def test_only_verified_found_browser_results_can_enter_history(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history")
    evidence = found(tmp_path)
    for rejected in [
        replace(evidence, status="partial_capture"), replace(evidence, database="gnomAD"),
        replace(evidence, raw={**evidence.raw, "assembly_verified": "GRCh38"}),
        replace(evidence, raw={**evidence.raw, "identity_verification": {"accepted": False}}),
        replace(evidence, raw={**evidence.raw, "historical_result": {"captured_at": CAPTURED.isoformat()}}),
        replace(evidence, raw={**evidence.raw, "screenshots": []}),
    ]:
        assert not store.record(variant(), rejected)


def test_verification_cannot_describe_different_genomic_identity(tmp_path):
    evidence = found(tmp_path, identity_verification={"accepted": True, "returned": {
        "assembly": "GRCh37", "chromosome": "7", "position": 999,
        "reference": "A", "alternate": "G",
    }})
    assert not EvidenceHistoryStore(tmp_path / "history").record(variant(), evidence)


@pytest.mark.parametrize("database", ["clinvar", "CLINVAR", "mtbp", "Mtbp"])
def test_provider_casing_cannot_bypass_verification_rules(tmp_path, database):
    evidence = found(tmp_path)
    evidence.database = database
    evidence.raw["assembly_verified"] = "GRCh38"
    assert not EvidenceHistoryStore(tmp_path / "history").record(variant(), evidence, context="Blood")


def test_mtbp_never_archives_full_patient_report_as_a_variant_capture(tmp_path):
    evidence = found(tmp_path, "MTBP", identity_matching_version=2, match_basis="cdna", cancer_type="Blood")
    evidence.raw["patient_report_screenshot"] = evidence.raw["screenshots"][0]["path"]
    assert not EvidenceHistoryStore(tmp_path / "history").record(variant(), evidence, context="Blood")


def test_mtbp_requires_exact_current_matching_and_same_cancer_context(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    evidence = found(tmp_path, "MTBP", identity_matching_version=2, match_basis="cdna", cancer_type="Blood",
        analysis_id="ARCHER-PRIVATE-OLD", submitted_query="PRIVATE OLD QUERY", patient_report_screenshot="private-full-report.png")
    assert not store.record(variant(), evidence)
    assert not store.record(variant(), evidence, context="Other")
    assert not store.record(variant(), replace(evidence, raw={**evidence.raw, "match_basis": "unique_gene_without_reported_identity"}), context="Blood")
    assert not store.record(variant(), replace(evidence, raw={**evidence.raw, "identity_matching_version": 1}), context="Blood")
    assert store.record(variant(), evidence, context="Blood")
    current = DatabaseEvidence("MTBP", "timeout", "current report pending", raw={
        "analysis_id": "ARCHER-CURRENT", "submitted_query": "CURRENT QUERY",
        "query_attempts": ["CURRENT QUERY"], "remote_report_cleanup": {"status": "failed"},
        "remote_report_recovery": {"status": "pending"}, "failure_kind": "timeout", "failure_stage": "report",
    })
    assert store.fallback(variant(), current, tmp_path / "out", context="Lung") is current
    result = store.fallback(variant(), current, tmp_path / "out", context=" blood ")
    assert result.status == "historical"
    for key, value in current.raw.items():
        assert result.raw[key] == value
    assert "PRIVATE-OLD" not in json.dumps(asdict(result))
    assert "private-full-report" not in json.dumps(asdict(result))


def test_storage_omits_private_and_full_report_data(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history")
    evidence = found(tmp_path, patient="PRIVATE_PATIENT", sample="PRIVATE_SAMPLE", source_file="PRIVATE_FILE",
        source_row=999, visible_text_preview="PRIVATE_BODY", analysis_id="PRIVATE_ANALYSIS", password="PRIVATE_PASSWORD",
        patient_report_screenshot="PRIVATE_FULL_REPORT", unknown_nested={"sample": "PRIVATE_NESTED"})
    assert store.record(variant(), evidence)
    persisted = b"".join(p.read_bytes() for p in (tmp_path / "history").glob("*.sqlite*"))
    assert b"Pathogenic" in persisted
    for forbidden in [b"PRIVATE_PATIENT", b"PRIVATE_SAMPLE", b"PRIVATE_FILE", b"PRIVATE_BODY", b"PRIVATE_ANALYSIS", b"PRIVATE_PASSWORD", b"PRIVATE_FULL_REPORT", b"PRIVATE_NESTED"]:
        assert forbidden not in persisted


def test_latest_original_capture_wins_without_refreshing_duplicate_timestamp(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    evidence = found(tmp_path)
    assert store.record(variant(), evidence)
    assert store.record(variant(), evidence)
    failed = DatabaseEvidence("ClinVar", "not_found")
    result = store.fallback(variant(), failed, tmp_path / "out")
    assert result.raw["captured_at"] == CAPTURED.isoformat()
    newer = replace(evidence, summary="updated classification", clinical_significance="Likely pathogenic")
    later = CAPTURED + timedelta(days=2)
    assert store.record(variant(), newer, captured_at=later.astimezone(timezone(timedelta(hours=2))))
    result = store.fallback(variant(), failed, tmp_path / "out")
    assert result.clinical_significance == "Likely pathogenic"
    assert result.raw["captured_at"] == later.isoformat()


def test_genuinely_new_identical_capture_can_update_last_observed_time(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    evidence = found(tmp_path)
    assert store.record(variant(), evidence)
    fresh = replace(evidence, raw={**evidence.raw, "captured_at": NOW.isoformat()})
    assert store.record(variant(), fresh)
    restored = store.fallback(variant(), DatabaseEvidence("ClinVar", "timeout"), tmp_path / "out")
    assert restored.raw["captured_at"] == NOW.isoformat()


def test_cosmic_requires_actual_capture_proof_or_explicit_verification(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    evidence = found(tmp_path, "COSMIC")
    evidence.raw.pop("identity_verification")
    evidence.accession = "COSM1234"
    synthetic = variant(cosmic_id="COSM1234")
    assert not store.record(synthetic, evidence)
    evidence.raw["screenshots"] = [
        {"label": label, "path": str(capture(tmp_path / f"cosmic-{index}.png", label))}
        for index, label in enumerate([
            "COSMIC overview", "COSMIC tissue distribution", "COSMIC samples filtered to lymphoid",
        ])
    ]
    assert not store.record(variant(cosmic_id="COSM9999"), evidence)
    assert store.record(synthetic, evidence)
    assert store.fallback(synthetic, DatabaseEvidence("COSMIC", "error"), tmp_path / "out").status == "historical"


def test_mixed_valid_and_missing_capture_is_not_recorded(tmp_path):
    evidence = found(tmp_path)
    evidence.raw["screenshots"].append({"label": "Required second panel", "path": ""})
    assert not EvidenceHistoryStore(tmp_path / "history").record(variant(), evidence)


def test_output_folder_failure_is_reported_as_controlled_error(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history")
    assert store.record(variant(), found(tmp_path))
    target = tmp_path / "not-a-directory"
    target.write_text("existing file")
    with pytest.raises(EvidenceHistoryError):
        store.fallback(variant(), DatabaseEvidence("ClinVar", "error"), target)


def test_missing_timestamp_uses_clock_but_invalid_timestamp_is_rejected(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    evidence = found(tmp_path)
    evidence.raw.pop("captured_at")
    assert store.record(variant(), evidence)
    result = store.fallback(variant(), DatabaseEvidence("ClinVar", "error"), tmp_path / "out")
    assert result.raw["captured_at"] == NOW.isoformat()
    assert not store.record(variant(), replace(evidence, raw={**evidence.raw, "captured_at": "bad"}))
    assert not store.record(variant(), evidence, captured_at=datetime(2026, 10, 1))


def test_historical_evidence_is_not_cached_again(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    assert store.record(variant(), found(tmp_path))
    historical = store.fallback(variant(), DatabaseEvidence("ClinVar", "error"), tmp_path / "out")
    assert not store.record(variant(), historical)
    assert store.fallback(variant(), historical, tmp_path / "out") is historical


def test_missing_or_corrupted_history_capture_does_not_supply_backup(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history")
    assert store.record(variant(), found(tmp_path))
    for path in (tmp_path / "history" / "assets").iterdir():
        path.write_bytes(b"corrupted")
    failed = DatabaseEvidence("ClinVar", "error")
    assert store.fallback(variant(), failed, tmp_path / "out") is failed


def test_constructor_and_empty_lookup_are_lazy(tmp_path):
    directory = tmp_path / "history"
    store = EvidenceHistoryStore(directory)
    assert not directory.exists()
    failed = DatabaseEvidence("ClinVar", "error")
    assert store.fallback(variant(), failed, tmp_path / "out") is failed
    assert not directory.exists()


def test_corrupt_database_raises_controlled_error_without_changing_result(tmp_path):
    directory = tmp_path / "history"
    directory.mkdir()
    (directory / "history.sqlite3").write_bytes(b"this is not SQLite")
    store = EvidenceHistoryStore(directory)
    with pytest.raises(EvidenceHistoryError):
        store.record(variant(), found(tmp_path))
    with pytest.raises(EvidenceHistoryError):
        store.fallback(variant(), DatabaseEvidence("ClinVar", "error"), tmp_path / "out")


def test_storage_permissions_raise_controlled_error(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(sqlite3, "connect", denied)
    with pytest.raises(EvidenceHistoryError):
        EvidenceHistoryStore(tmp_path / "history").record(variant(), found(tmp_path))


def test_concurrent_records_do_not_mix_variants_or_duplicate_age(tmp_path):
    store = EvidenceHistoryStore(tmp_path / "history", clock=lambda: NOW)
    evidence = found(tmp_path)
    def save(index):
        return store.record(variant(sample=f"SAMPLE_{index}"), evidence)
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(save, range(24)))
    with sqlite3.connect(tmp_path / "history" / "history.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    restored = store.fallback(variant(), DatabaseEvidence("ClinVar", "error"), tmp_path / "out")
    assert CAPTURED <= datetime.fromisoformat(restored.raw["captured_at"]) < CAPTURED + timedelta(days=1)


def test_concurrent_store_instances_initialize_and_write_without_lock_errors(tmp_path):
    evidence = found(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        for generation in range(10):
            directory = tmp_path / f"history-{generation}"
            start = Barrier(8)
            def save(index):
                store = EvidenceHistoryStore(directory, clock=lambda: NOW)
                start.wait(timeout=10)
                return store.record(variant(genomic_location=f"chr7:{12345 + index}"), evidence)
            assert all(executor.map(save, range(8)))
            with sqlite3.connect(directory / "history.sqlite3") as connection:
                assert connection.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 8
                assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_current_attempt_is_identity_for_regular_evidence_and_rejects_malformed_metadata():
    regular = DatabaseEvidence("MTBP", "timeout", raw={"analysis_id": "ARCHER-CURRENT"})
    assert current_attempt(regular) is regular
    malformed = DatabaseEvidence("MTBP", "historical", raw={"historical_result": {"current_attempt": {"database": "COSMIC", "status": "found"}}})
    assert current_attempt(malformed) is malformed
    malformed = DatabaseEvidence("MTBP", "historical", raw={"historical_result": {"current_attempt": {"database": "MTBP", "status": "timeout", "raw": []}}})
    assert current_attempt(malformed) is malformed
