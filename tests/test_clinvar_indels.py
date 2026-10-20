from dataclasses import replace
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewService,
    _clinvar_identity,
    _clinvar_queries,
)


def variant():
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1, sample="SYNTHETIC",
        symbol="TESTGENE", hgvsc="NM_999999.1:c.100_101del",
        hgvsp="", genomic_location="chr17:1000", ref_allele="ACT", alt_allele="A",
    )


def body(location="17: 1,000-1,002 (GRCh37)"):
    return (
        "NM_999999.1(TESTGENE):c.100_101del\n"
        "Variation ID: 999999 Accession: VCV000999999.1\n"
        f"Location\n{location}"
    )


class Page:
    url = "https://www.ncbi.nlm.nih.gov/clinvar/variation/999999/"

    def __init__(self, text):
        self.text = text
        self.waits = []

    def locator(self, selector):
        page = self

        class Locator:
            def inner_text(self, **kwargs):
                return page.text

            def evaluate_all(self, script):
                return []

        return Locator()

    def goto(self, url, **kwargs):
        pass  # A single exact search redirects to the synthetic record.

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


def test_completed_indel_record_is_verified_and_captured(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    page = Page(body())
    monkeypatch.setattr(service, "_capture_clinvar_result", lambda v, e, p, d: e)
    result = service._lookup_clinvar_variant(page, variant(), tmp_path)
    assert result.status == "found"
    assert result.raw["identity_verification"]["accepted"]
    assert page.waits == []


@pytest.mark.parametrize("location", [
    "17: 1000-1002 (GRCh37)",  # Full VCF reference span, including anchor.
    "17: 1001-1002 (GRCh37)",  # Deleted bases after removing the VCF anchor.
    "17: 1,000 – 1,002 (GRCh37)",
])
def test_deletion_accepts_exact_reference_or_trimmed_span(location):
    assert _clinvar_identity(body(location), variant()).accepted


@pytest.mark.parametrize("location", [
    "17: 999-1003 (GRCh37)", "17: 1000-1003 (GRCh37)",
    "17: 1000 (GRCh37)", "17: 1001-1002 (GRCh38)",
    "18: 1001-1002 (GRCh37)",
])
def test_deletion_rejects_overlap_wrong_length_assembly_or_chromosome(location):
    assert not _clinvar_identity(body(location), variant()).accepted


def test_insertion_accepts_flanking_bases_but_still_requires_exact_change():
    requested = replace(variant(), ref_allele="A", alt_allele="ACT", hgvsc="NM_999999.1:c.100_101insCT")
    text = body("17: 1000-1001 (GRCh37)").replace("c.100_101del", "c.100_101insCT")
    assert _clinvar_identity(text, requested).accepted
    assert not _clinvar_identity(text.replace("insCT", "insGT"), requested).accepted


def test_indel_search_starts_with_assembly_and_alleles_and_skips_unknown_protein():
    requested = replace(variant(), hgvsp="p.?")
    queries = _clinvar_queries(requested)
    assert queries[0] == "17:1000:ACT:A(GRCh37)"
    assert "TESTGENE ?" not in queries
    assert "TESTGENE c.100_101del" in queries


def test_completed_grch38_record_is_rejected_without_render_timeout(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    page = Page(body("17: 1001-1002 (GRCh38)"))
    text, links = service._wait_for_clinvar_result(page)
    assert text == page.text
    assert not _clinvar_identity(text, variant()).accepted
    assert not page.waits


def test_timeout_identifies_missing_record_fields_without_dumping_page(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    page = Page("Variation ID: 999999\nLoading...")
    result = service._lookup_clinvar_variant(page, variant(), tmp_path)
    assert result.status == "error"
    assert result.raw["render_diagnostics"] == {
        "page_type": "record", "record_metadata": True,
        "hgvs_title": False, "genomic_location": False, "result_links": 0,
    }
    assert "Loading" not in str(result.raw)


def test_explicit_no_results_were_found_allows_fallback(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    page = Page("No results were found for your search.")
    page.url = "https://www.ncbi.nlm.nih.gov/clinvar/?term=synthetic"
    text, links = service._wait_for_clinvar_result(page)
    assert text == page.text and links == []
    assert not page.waits


def test_render_diagnostics_are_visible_in_activity_log_without_page_content(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    requested = variant()
    messages = []
    service._report_provider_results(
        "ClinVar", [requested], {service.variant_key(requested): DatabaseEvidence(
            "ClinVar", "error", "Rendering timeout", raw={
                "query_attempts": ["synthetic exact query"],
                "render_diagnostics": {"page_type": "record", "record_metadata": True,
                    "hgvs_title": False, "genomic_location": False, "result_links": 0},
            },
        )}, duration_seconds=1, progress=messages.append,
    )
    result = next(message for message in messages if message.startswith("RESULT |"))
    assert "attempts=1" in result
    assert "page_type=record" in result and "hgvs_title=no" in result
    assert "synthetic exact query" not in result


@pytest.mark.parametrize("location", ["17: , (GRCh37)", "17: 1000-999 (GRCh37)"])
def test_malformed_locations_do_not_crash_or_verify(location):
    assert not _clinvar_identity(body(location), variant()).accepted


def test_duplication_requires_exact_change_and_insertion_flanks():
    requested = replace(variant(), ref_allele="A", alt_allele="ACT", hgvsc="NM_999999.1:c.99_100dup")
    text = body("17: 1000-1001 (GRCh37)").replace("c.100_101del", "c.99_100dup")
    assert _clinvar_identity(text, requested).accepted
    assert not _clinvar_identity(text.replace("99_100dup", "98_100dup"), requested).accepted


def test_right_anchored_deletion_accepts_only_exact_deleted_bases():
    requested = replace(variant(), ref_allele="CTA", alt_allele="A")
    assert _clinvar_identity(body("17: 1000-1001 (GRCh37)"), requested).accepted
    assert not _clinvar_identity(body("17: 1001-1002 (GRCh37)"), requested).accepted


def test_delins_requires_exact_span_and_cdna():
    requested = replace(variant(), ref_allele="ACT", alt_allele="AG", hgvsc="NM_999999.1:c.100_101delinsG")
    text = body("17: 1001-1002 (GRCh37)").replace("c.100_101del", "c.100_101delinsG")
    assert _clinvar_identity(text, requested).accepted
    assert not _clinvar_identity(text.replace("delinsG", "delinsT"), requested).accepted


def test_snp_is_not_accepted_in_a_wider_coordinate_range():
    requested = replace(variant(), ref_allele="A", alt_allele="T", hgvsc="NM_999999.1:c.100A>T")
    text = body("17: 1000-1002 (GRCh37)").replace("c.100_101del", "c.100A>T")
    assert not _clinvar_identity(text, requested).accepted


def test_record_timeout_keeps_one_query_and_does_not_blindly_retry(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    result = service._lookup_clinvar_variant(Page("Loading..."), variant(), tmp_path)
    assert result.raw["query_attempts"] == ["17:1000:ACT:A(GRCh37)"]
    assert result.status == "error"
    assert result.raw["failure_kind"] == "transient"
