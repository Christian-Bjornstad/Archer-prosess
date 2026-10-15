import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.evidence_audit import (
    EvidenceAuditIndex,
    audit_digest,
    write_evidence_audit,
)
from archer_processor.services.processed_workbook import ProcessedWorkbookLoader


def variant():
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1, sample="DEMO_A_VPM_1",
        symbol="TP53", hgvsc="NM_000546.6:c.524G>A", hgvsp="p.Arg175His",
        genomic_location="chr17:7578406", ref_allele="G", alt_allele="A",
    )


def legacy_digest(database, candidate):
    identity = f"{database}|{candidate.symbol}|{candidate.hgvsc}|{candidate.hgvsp}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def write_legacy(directory, candidate, *, sample=None, requested_genomic=None):
    payload = asdict(DatabaseEvidence("MTBP", "not_found", "Legacy result."))
    if sample is not None:
        payload["variant_key"] = f"{sample}|{candidate.hgvsc}"
    if requested_genomic is not None:
        payload["raw"]["identity_verification"] = {"requested": requested_genomic}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{legacy_digest('MTBP', candidate)}.audit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_restore_keeps_patient_specific_outcomes_for_a_shared_variant(tmp_path):
    first = variant()
    second = replace(first, sample="DEMO_B_VPM_1", source_row=2)
    workbook = tmp_path / "review.xlsx"
    root = tmp_path / "review_browser_evidence"
    first_path = write_evidence_audit(
        root / "patient-001" / "mtbp", "MTBP", first,
        DatabaseEvidence("MTBP", "error", "Patient A pending.", raw={"analysis_id": "ARCHER-PATIENT-A"}),
    )
    second_path = write_evidence_audit(
        root / "patient-002" / "mtbp", "MTBP", second,
        DatabaseEvidence("MTBP", "not_found", "Patient B absent.", raw={"analysis_id": "ARCHER-PATIENT-B"}),
    )
    newer = first_path.stat().st_mtime + 2
    os.utime(second_path, (newer, newer))
    cells = {BrowserReviewService.variant_key(item): {"MTBP": ""} for item in (first, second)}

    restored = ProcessedWorkbookLoader()._restore_evidence(workbook, [first, second], cells)

    left = restored[BrowserReviewService.variant_key(first)][0]
    right = restored[BrowserReviewService.variant_key(second)][0]
    assert (left.status, left.raw["analysis_id"]) == ("error", "ARCHER-PATIENT-A")
    assert (right.status, right.raw["analysis_id"]) == ("not_found", "ARCHER-PATIENT-B")


@pytest.mark.parametrize("changes", [{"genomic_location": "chr17:7578407"}, {"ref_allele": "T"}, {"alt_allele": "C"}])
def test_different_genomic_identities_do_not_overwrite_the_same_audit(tmp_path, changes):
    first = variant()
    second = replace(first, **changes)
    directory = tmp_path / "patient-001" / "clinvar"
    left = write_evidence_audit(directory, "ClinVar", first, DatabaseEvidence("ClinVar", "error", "First identity."))
    right = write_evidence_audit(directory, "ClinVar", second, DatabaseEvidence("ClinVar", "not_found", "Second identity."))

    index = EvidenceAuditIndex.build(tmp_path)
    first_record = index.best("ClinVar", first)
    second_record = index.best("ClinVar", second)

    assert left != right
    assert first_record[1]["summary"] == "First identity."
    assert second_record[1]["summary"] == "Second identity."


def test_exact_patient_identity_survives_patient_directory_renumbering(tmp_path):
    candidate = variant()
    root = tmp_path / "review_browser_evidence"
    write_evidence_audit(
        root / "patient-009" / "mtbp", "MTBP", candidate,
        DatabaseEvidence("MTBP", "not_found", "Saved exact patient identity."),
    )
    cells = {BrowserReviewService.variant_key(candidate): {"MTBP": ""}}

    restored = ProcessedWorkbookLoader()._restore_evidence(tmp_path / "review.xlsx", [candidate], cells)

    assert restored[BrowserReviewService.variant_key(candidate)][0].summary == "Saved exact patient identity."


def test_legacy_audits_without_patient_identity_are_limited_to_expected_directory(tmp_path):
    candidate = variant()
    root = tmp_path / "review_browser_evidence"
    own = write_legacy(root / "patient-001" / "mtbp", candidate)
    other = write_legacy(root / "patient-002" / "mtbp", candidate)
    other_payload = json.loads(other.read_text(encoding="utf-8"))
    other_payload["summary"] = "Other patient result."
    other.write_text(json.dumps(other_payload), encoding="utf-8")
    newer = own.stat().st_mtime + 2
    os.utime(other, (newer, newer))
    cells = {BrowserReviewService.variant_key(candidate): {"MTBP": ""}}

    restored = ProcessedWorkbookLoader()._restore_evidence(tmp_path / "review.xlsx", [candidate], cells)

    assert restored[BrowserReviewService.variant_key(candidate)][0].summary == "Legacy result."


def test_legacy_audit_with_explicit_other_patient_is_not_restored(tmp_path):
    candidate = variant()
    root = tmp_path / "review_browser_evidence"
    write_legacy(root / "patient-001" / "mtbp", candidate, sample="DEMO_B_VPM_1")
    cells = {BrowserReviewService.variant_key(candidate): {"MTBP": ""}}

    restored = ProcessedWorkbookLoader()._restore_evidence(tmp_path / "review.xlsx", [candidate], cells)

    assert restored == {}


@pytest.mark.parametrize("changes", [{"position": 7578407}, {"reference": "T"}, {"alternate": "C"}])
def test_legacy_audit_rejects_explicit_conflicting_genomic_identity(tmp_path, changes):
    candidate = variant()
    requested = {"assembly": "GRCh37", "chromosome": "17", "position": 7578406, "reference": "G", "alternate": "A"}
    requested.update(changes)
    root = tmp_path / "review_browser_evidence"
    write_legacy(root / "patient-001" / "mtbp", candidate, sample=candidate.sample, requested_genomic=requested)
    cells = {BrowserReviewService.variant_key(candidate): {"MTBP": ""}}

    restored = ProcessedWorkbookLoader()._restore_evidence(tmp_path / "review.xlsx", [candidate], cells)

    assert restored == {}


def test_saved_requested_identity_is_checked_even_when_filename_matches(tmp_path):
    candidate = variant()
    root = tmp_path / "patient-001" / "mtbp"
    path = write_evidence_audit(root, "MTBP", candidate, DatabaseEvidence("MTBP", "not_found", "Saved result."))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["requested_identity"] = {
        "sample": "DEMO_B_VPM_1", "symbol": "TP53", "hgvsc": candidate.hgvsc,
        "hgvsp": candidate.hgvsp, "genomic_location": "chr17:7578406",
        "ref_allele": "G", "alt_allele": "A",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert EvidenceAuditIndex.build(tmp_path).best("MTBP", candidate) is None
