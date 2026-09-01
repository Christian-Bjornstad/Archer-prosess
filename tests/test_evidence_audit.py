import json
from pathlib import Path

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.evidence_audit import (
    RETRYABLE_EVIDENCE_STATUSES,
    audit_digest,
    is_completed_evidence,
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


def test_mtbp_cleanup_failure_remains_retryable_without_losing_found_result():
    failed = DatabaseEvidence(
        "MTBP",
        "found",
        "Validated evidence",
        raw={"remote_report_cleanup": {"status": "failed"}},
    )
    deleted = DatabaseEvidence(
        "MTBP",
        "found",
        "Validated evidence",
        raw={"remote_report_cleanup": {"status": "deleted"}},
    )
    absent = DatabaseEvidence(
        "MTBP",
        "found",
        "Validated evidence",
        raw={"remote_report_cleanup": {"status": "already_absent"}},
    )

    assert not is_completed_evidence(failed)
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
    assert payload["schema_version"] == 2
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
