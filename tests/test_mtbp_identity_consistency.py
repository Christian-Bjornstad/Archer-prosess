from dataclasses import replace
from pathlib import Path

import pytest

from archer_processor.core.models import VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewService,
    _mtbp_screenshot_row_matches,
    parse_mtbp_report,
)


def variant(**changes):
    base = VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1, sample="SYNTHETIC",
        symbol="TESTGENE", hgvsc="NM_999999.1:c.100del", hgvsp="p.Arg100Ser",
        genomic_location="chr17:1000", ref_allele="AC", alt_allele="A",
    )
    return replace(base, **changes)


def row(identity, *, alteration=None):
    return {
        "gene": "TESTGENE", "identity_text": identity,
        "alteration": identity if alteration is None else alteration,
        "section": "Reported variants", "functional_evidence": "Evidence B",
        "biomarkers": "", "source_links": [],
    }


def lookup(rows, variants, **kwargs):
    return parse_mtbp_report("", rows, variants, "synthetic", cancer_type="Blood", **kwargs)


@pytest.mark.parametrize("identity,basis", [
    ("NM_999999.2:c.100del", "cdna"),
    ("chr17:g.1001del (GRCh37)", "genomic_grch37"),
])
def test_report_without_protein_uses_verified_dna_for_parser_and_capture(identity, basis):
    requested = variant()
    evidence = lookup([row(identity)], [requested])[BrowserReviewService.variant_key(requested)]
    assert evidence.status == "found"
    assert evidence.raw["match_basis"] == basis
    assert _mtbp_screenshot_row_matches("TESTGENE " + identity, requested)


def test_cdna_in_alteration_is_available_to_parser_and_capture():
    requested = variant(hgvsp="")
    report = row("", alteration="NM_999999.1:c.100del")
    evidence = lookup([report], [requested])[BrowserReviewService.variant_key(requested)]
    assert evidence.status == "found"
    assert _mtbp_screenshot_row_matches("TESTGENE " + report["alteration"], requested)


def test_known_protein_fallback_requires_qualified_cdna_without_exact_genomic_identity():
    requested = variant()
    evidence = lookup([row("c.100del")], [requested])[BrowserReviewService.variant_key(requested)]
    assert evidence.status == "not_found"
    assert not _mtbp_screenshot_row_matches("TESTGENE c.100del", requested)


def test_capture_target_uses_identity_from_alteration_field(tmp_path, monkeypatch):
    requested = variant(hgvsp="")
    report = {**row("", alteration="NM_999999.1:c.100del"), "row_index": 0}
    service = BrowserReviewService(profile_root=tmp_path)
    monkeypatch.setattr(service, "_extract_mtbp_rows", lambda page: [report])

    class Target:
        def is_visible(self):
            return True

        def locator(self, selector):
            return self

        def count(self):
            return 0

        def wait_for(self, **kwargs):
            pass

    target = Target()

    class Rows:
        def nth(self, index):
            assert index == 0
            return target

    class Page:
        def locator(self, selector):
            assert selector == "table tr"
            return Rows()

    assert service._locate_mtbp_screenshot_target(Page(), requested) == (target, target)


@pytest.mark.parametrize("identity,alteration", [
    ("p.Arg100Ser", "p.Arg100Ser chr17:g.2001del (GRCh37)"),
    ("p.Arg100Gly", "NM_999999.1:c.100del chr17:g.1001del (GRCh37)"),
    ("NM_888888.1:c.100del", "NM_888888.1:c.100del"),
])
def test_explicit_conflicts_are_not_overridden_by_other_identity_fields(identity, alteration):
    requested = variant()
    evidence = lookup([row(identity, alteration=alteration)], [requested])[
        BrowserReviewService.variant_key(requested)
    ]
    assert evidence.status == "not_found"
    assert not _mtbp_screenshot_row_matches("TESTGENE " + identity + " " + alteration, requested)


def test_genomic_only_input_requires_exact_identity():
    requested = variant(hgvsc="", hgvsp="")
    found = lookup([row("chr17:g.1001del (GRCh37)")], [requested])[
        BrowserReviewService.variant_key(requested)
    ]
    rejected = lookup([row("p.Arg999Gly")], [requested])[
        BrowserReviewService.variant_key(requested)
    ]
    assert found.status == "found"
    assert found.raw["match_basis"] == "genomic_grch37"
    assert rejected.status == "not_found"


def test_input_without_any_variant_identity_cannot_import_gene_evidence():
    requested = variant(hgvsc="", hgvsp="", genomic_location="", ref_allele="", alt_allele="")
    evidence = lookup([row("p.Arg999Gly")], [requested])[BrowserReviewService.variant_key(requested)]
    assert evidence.status == "invalid_query"
    assert evidence.clinical_significance == ""


def test_identity_free_row_cannot_verify_two_distinct_submitted_variants_in_same_gene():
    first = variant(hgvsp="")
    second = replace(first, source_row=2, hgvsc="NM_999999.1:c.200del", genomic_location="chr17:2000")
    results = lookup([row("Mutation upstream gene -")], [first, second])
    for requested in (first, second):
        evidence = results[BrowserReviewService.variant_key(requested)]
        assert evidence.status == "manual_review"
        assert evidence.clinical_significance == ""
        assert evidence.raw["submitted_gene_variant_count"] == 2


def test_identity_free_row_retains_legacy_single_input_match():
    requested = variant(hgvsp="")
    duplicate = replace(requested, source_row=2)
    evidence = lookup([row("Mutation upstream gene -")], [requested, duplicate])[
        BrowserReviewService.variant_key(requested)
    ]
    assert evidence.status == "found"
    assert evidence.raw["match_basis"] == "unique_gene_without_reported_identity"
    assert evidence.raw["submitted_gene_variant_count"] == 1


def test_retained_report_without_complete_submission_cannot_use_gene_only_fallback():
    requested = variant(hgvsp="")
    evidence = lookup([row("Mutation upstream gene -")], [requested], complete_submission=False)[
        BrowserReviewService.variant_key(requested)
    ]
    assert evidence.status == "manual_review"
    assert evidence.clinical_significance == ""


def test_reported_row_without_identity_is_unresolved_when_input_has_known_protein():
    requested = variant()
    evidence = lookup([row("Mutation splice region more")], [requested])[
        BrowserReviewService.variant_key(requested)
    ]
    assert evidence.status == "manual_review"
    assert evidence.raw["gene_candidate_count"] == 1


def test_multiple_exact_matching_rows_are_terminal_manual_review():
    requested = variant()
    evidence = lookup([row("p.Arg100Ser"), row("p.Arg100Ser")], [requested])[
        BrowserReviewService.variant_key(requested)
    ]
    assert evidence.status == "manual_review"
    assert evidence.raw["candidate_count"] == 2


def test_mtbp_audit_records_matching_algorithm_revision():
    requested = variant()
    evidence = lookup([row("p.Arg100Ser")], [requested])[BrowserReviewService.variant_key(requested)]
    assert evidence.raw["identity_matching_version"] == 2
