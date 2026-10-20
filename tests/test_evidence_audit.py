import json
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.evidence_audit import (
    RETRYABLE_EVIDENCE_STATUSES,
    audit_digest,
    is_completed_evidence,
    migrate_loaded_evidence,
    persist_evidence_result,
    write_evidence_audit,
)
from archer_processor.services.variant_identity import GenomicIdentity, genomic_identity


def variant() -> VariantRecord:
    return VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=2,
        sample="SYNTHETIC_VPM_1",
        symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup",
        genomic_location="chr7:139097298",
        ref_allele="T",
        alt_allele="TC",
    )


def test_grch37_identity_normalizes_archer_fields():
    assert genomic_identity(variant()) == GenomicIdentity(
        "GRCh37", "7", 139097298, "T", "TC"
    )


def test_retryable_evidence_is_not_treated_as_completed():
    expected = {
        "error",
        "login_required",
        "rate_limited",
        "timeout",
        "unauthorized",
        "identity_mismatch",
        "partial_capture",
        "verification_required",
        "quota_exhausted",
        "session_lost",
    }
    assert expected <= RETRYABLE_EVIDENCE_STATUSES
    for status in expected:
        assert not is_completed_evidence(DatabaseEvidence("Franklin", status))
    assert is_completed_evidence(DatabaseEvidence("Franklin", "found"))
    assert is_completed_evidence(DatabaseEvidence("Franklin", "not_found"))
    assert is_completed_evidence(DatabaseEvidence("OncoKB", "manual_review"))
    assert not is_completed_evidence(DatabaseEvidence("OncoKB", "unknown_status"))
    assert not is_completed_evidence(DatabaseEvidence("OncoKB", ""))
    assert not is_completed_evidence(DatabaseEvidence("OncoKB", "token_required"))


def test_mtbp_cleanup_failure_remains_retryable_without_losing_found_result():
    failed = DatabaseEvidence(
        "MTBP",
        "found",
        "Validated evidence",
        raw={
            "analysis_id": "ARCHER-failed",
            "remote_report_cleanup": {"status": "failed"},
        },
    )
    persisted_before_cleanup = DatabaseEvidence(
        "MTBP",
        "found",
        "Validated evidence",
        raw={"analysis_id": "ARCHER-persisted"},
    )
    deleted = DatabaseEvidence(
        "MTBP",
        "found",
        "Validated evidence",
        raw={
            "analysis_id": "ARCHER-deleted",
            "remote_report_cleanup": {"status": "deleted"},
        },
    )
    absent = DatabaseEvidence(
        "MTBP",
        "found",
        "Validated evidence",
        raw={
            "analysis_id": "ARCHER-absent",
            "remote_report_cleanup": {"status": "already_absent"},
        },
    )

    assert not is_completed_evidence(failed)
    assert not is_completed_evidence(persisted_before_cleanup)
    assert is_completed_evidence(deleted)
    assert is_completed_evidence(absent)


def test_canonical_audit_contains_resume_metadata(tmp_path):
    evidence = DatabaseEvidence("Franklin", "identity_mismatch", "wrong candidate")
    path = write_evidence_audit(
        tmp_path,
        "Franklin",
        variant(),
        evidence,
        query_attempts=["LUC7L2:c.784dup", "chr7-139097298 T>TC"],
        duration_seconds=2.5,
    )

    assert path.name == f"{audit_digest('Franklin', variant())}.audit.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["retryable"] is True
    assert payload["variant_key"] == "SYNTHETIC_VPM_1|NM_016019.4:c.784dup"
    assert payload["query_attempts"] == [
        "LUC7L2:c.784dup",
        "chr7-139097298 T>TC",
    ]
    assert payload["duration_seconds"] == 2.5
    assert payload["written_at"].endswith("+00:00")


def test_persist_evidence_result_returns_evidence_and_writes_failure(tmp_path):
    evidence = DatabaseEvidence("MTBP", "timeout", "still processing")

    returned = persist_evidence_result(
        tmp_path,
        "MTBP",
        variant(),
        evidence,
        query_attempts=["NM_016019.4:c.784dup"],
        started_at=0.0,
        now=lambda: 3.0,
    )

    assert returned is evidence
    payload = json.loads(
        (tmp_path / f"{audit_digest('MTBP', variant())}.audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["status"] == "timeout"
    assert payload["duration_seconds"] == 3.0


@pytest.mark.parametrize("matching_version", [None, 0, 1])
def test_legacy_mtbp_gene_rows_without_match_are_reopened_for_verification(
    tmp_path, matching_version,
):
    raw = {
        "gene_candidate_count": 2,
        "candidate_count": 0,
        "match_basis": "cdna",
        "analysis_id": "ARCHER-synthetic-retained",
        "submitted_query": "NM_999999.1:c.123+1G>A",
    }
    if matching_version is not None:
        raw["identity_matching_version"] = matching_version
    evidence = DatabaseEvidence("MTBP", "not_found", "No matching row.", raw=raw.copy())

    restored = migrate_loaded_evidence(evidence, tmp_path)

    assert restored.status == "verification_required"
    assert "MTBP" in restored.summary
    assert "recheck" in restored.summary.casefold()
    assert not is_completed_evidence(restored)
    assert restored.raw == raw


@pytest.mark.parametrize(
    "database,raw",
    [
        ("MTBP", {"gene_candidate_count": 0}),
        ("MTBP", {}),
        ("MTBP", {"gene_candidate_count": 2, "identity_matching_version": 2}),
        ("MTBP", {"gene_candidate_count": 2, "identity_matching_version": 3}),
        ("ClinVar", {"gene_candidate_count": 2}),
    ],
)
def test_confirmed_or_unrelated_nohits_are_not_reopened(tmp_path, database, raw):
    evidence = DatabaseEvidence(database, "not_found", "Existing result.", raw=raw)

    restored = migrate_loaded_evidence(evidence, tmp_path)

    assert restored.status == "not_found"
    assert restored.summary == "Existing result."
    assert is_completed_evidence(restored)


@pytest.mark.parametrize("status", ["ambiguous", "ambiguous_result"])
def test_ambiguous_results_allow_explicit_retry_without_automatic_repetition(status):
    from archer_processor.gui.app import _failed_search_variants, _retryable_source_pairs

    candidate = variant()
    key = f"{candidate.sample}|{candidate.hgvsc}"
    evidence = {key: [DatabaseEvidence("MTBP", status, "Multiple possible rows.")]}

    assert not is_completed_evidence(evidence[key][0])
    assert _retryable_source_pairs([candidate], evidence, ["MTBP"]) == {(key, "MTBP")}
    assert _failed_search_variants([candidate], evidence, ["MTBP"]) == []


def test_reopened_mtbp_audit_reenters_resume_queue_only_until_new_matching_pass(tmp_path):
    from archer_processor.gui.app import _completed_evidence_sources, _retryable_source_pairs
    from archer_processor.services.processed_workbook import ProcessedWorkbookLoader

    candidate = variant()
    key = f"{candidate.sample}|{candidate.hgvsc}"
    directory = tmp_path / "review_browser_evidence" / "patient-001" / "mtbp"
    write_evidence_audit(
        directory, "MTBP", candidate,
        DatabaseEvidence("MTBP", "not_found", "Old unmatched gene row.", raw={
            "gene_candidate_count": 1, "match_basis": "cdna",
        }),
    )
    loader = ProcessedWorkbookLoader()

    reopened = loader._restore_evidence(
        tmp_path / "review.xlsx", [candidate], {key: {"MTBP": ""}},
    )

    assert reopened[key][0].status == "verification_required"
    assert (key, "MTBP") not in _completed_evidence_sources(reopened)
    assert _retryable_source_pairs([candidate], reopened, ["MTBP"]) == {(key, "MTBP")}

    write_evidence_audit(
        directory, "MTBP", candidate,
        DatabaseEvidence("MTBP", "not_found", "New checked gene row did not match.", raw={
            "gene_candidate_count": 1, "identity_matching_version": 2,
        }),
    )
    checked = loader._restore_evidence(
        tmp_path / "review.xlsx", [candidate], {key: {"MTBP": ""}},
    )

    assert checked[key][0].status == "not_found"
    assert (key, "MTBP") in _completed_evidence_sources(checked)
    assert _retryable_source_pairs([candidate], checked, ["MTBP"]) == set()


@pytest.mark.parametrize(
    "database,basis,matching_version,submitted_count,expected_status",
    [
        ("MTBP", "unique_gene_without_reported_identity", None, None, "verification_required"),
        ("MTBP", "unique_gene_without_reported_identity", 1, 1, "verification_required"),
        ("MTBP", "unique_gene_without_reported_identity", 2, None, "verification_required"),
        ("MTBP", "unique_gene_without_reported_identity", 2, 2, "verification_required"),
        ("MTBP", "unique_gene_without_reported_identity", 2, 1, "found"),
        ("MTBP", "unique_gene_without_reported_identity", 3, 1, "found"),
        ("MTBP", "protein", None, None, "found"),
        ("OncoKB", "unique_gene_without_reported_identity", None, None, "found"),
    ],
)
def test_mtbp_gene_only_result_requires_proven_single_variant_submission(
    tmp_path, database, basis, matching_version, submitted_count, expected_status,
):
    from PIL import Image, ImageDraw

    screenshot = tmp_path / "evidence.png"
    image = Image.new("RGB", (800, 500), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 780, 480), fill="navy")
    image.save(screenshot)
    raw = {
        "match_basis": basis,
        "screenshots": [{"path": str(screenshot)}],
        "remote_report_cleanup": {"status": "deleted"},
    }
    if matching_version is not None:
        raw["identity_matching_version"] = matching_version
    if submitted_count is not None:
        raw["submitted_gene_variant_count"] = submitted_count
    evidence = DatabaseEvidence(database, "found", "Existing gene-context result.", raw=raw)

    restored = migrate_loaded_evidence(evidence, tmp_path)

    assert restored.status == expected_status
    assert is_completed_evidence(restored) == (expected_status == "found")
