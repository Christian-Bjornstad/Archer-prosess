import json
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.evidence_audit import write_evidence_audit
from archer_processor.services.recent_analysis import inspect_recent_analysis


def test_recent_analysis_detects_local_retryable_schema_v2_evidence(tmp_path):
    workbook = tmp_path / "review.xlsx"
    workbook.write_bytes(b"synthetic")
    evidence = tmp_path / "review_browser_evidence" / "Franklin"
    evidence.mkdir(parents=True)
    (evidence / "synthetic.audit.json").write_text(
        json.dumps({"schema_version": 2, "retryable": True}),
        encoding="utf-8",
    )

    recent = inspect_recent_analysis(workbook)

    assert recent.valid
    assert recent.evidence_present
    assert recent.resume_available


@pytest.mark.parametrize("status,expected_resume", [("timeout", True), ("not_found", False)])
def test_recent_analysis_recognizes_audits_written_by_current_app(
    tmp_path, status, expected_resume
):
    workbook = tmp_path / "review.xlsx"
    workbook.write_bytes(b"synthetic")
    write_evidence_audit(
        tmp_path / "review_browser_evidence" / "clinvar",
        "ClinVar",
        VariantRecord(Path("synthetic.tsv"), 1, "SYNTHETIC", "TESTGENE", "c.100A>T"),
        DatabaseEvidence("ClinVar", status),
    )

    recent = inspect_recent_analysis(workbook)

    assert recent.valid
    assert recent.resume_available is expected_resume


def test_recent_analysis_ignores_malformed_audit_without_invalidating_workbook(
    tmp_path,
):
    workbook = tmp_path / "review.xlsx"
    workbook.write_bytes(b"synthetic")
    evidence = tmp_path / "review_browser_evidence"
    evidence.mkdir()
    (evidence / "broken.audit.json").write_text("{", encoding="utf-8")

    recent = inspect_recent_analysis(workbook)

    assert recent.valid
    assert recent.evidence_present
    assert not recent.resume_available
    assert "ignored" in recent.message.casefold()


def test_recent_analysis_rejects_missing_or_non_excel_paths(tmp_path):
    missing = inspect_recent_analysis(tmp_path / "missing.xlsx")
    text = tmp_path / "review.txt"
    text.write_text("synthetic", encoding="utf-8")
    non_excel = inspect_recent_analysis(text)

    assert not missing.valid
    assert not non_excel.valid
