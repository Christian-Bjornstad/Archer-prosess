from dataclasses import replace
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewService, _oncokb_query_urls, parse_mtbp_report,
    _mtbp_screenshot_row_matches,
    parse_oncokb_page,
)
from archer_processor.services.provider_failures import ProviderLookupError


def variant():
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1, sample="SYNTHETIC",
        symbol="TP53", hgvsc="NM_000546.6:c.524G>A", hgvsp="p.Arg175His",
        genomic_location="chr17:7578406", ref_allele="G", alt_allele="A",
        cosmic_id="COSM10648",
    )


@pytest.mark.parametrize("protein", ["p.?", "p.(?)", "p.=", "-"])
def test_oncokb_unknown_protein_uses_genomic_query_first(protein):
    queries = _oncokb_query_urls(replace(variant(), hgvsp=protein))
    assert "/hgvsg/" in queries[0]
    assert "refGenome=GRCh37" in queries[0]


def test_oncokb_render_timeout_tries_distinct_genomic_fallback(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    attempts = []

    def lookup(page, requested, url, directory):
        attempts.append(url)
        return DatabaseEvidence("OncoKB", "found" if "/hgvsg/" in url else "error", "synthetic", raw={
            "failure_kind": "transient", "failure_stage": "result_rendering",
        })

    monkeypatch.setattr(service, "_lookup_oncokb_url", lookup)
    result = service._lookup_oncokb_variant(object(), variant(), tmp_path)
    assert result.status == "found" and len(attempts) == 2
    assert result.raw["query_attempts"] == attempts


def test_oncokb_login_failure_does_not_try_other_urls(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    calls = []
    def lookup(*args):
        calls.append(1)
        return DatabaseEvidence("OncoKB", "login_required", "expired")
    monkeypatch.setattr(service, "_lookup_oncokb_url", lookup)
    assert service._lookup_oncokb_variant(object(), variant(), tmp_path).status == "login_required"
    assert calls == [1]


def test_cosmic_conflicting_identity_is_manual_after_all_ids(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    monkeypatch.setattr(service, "_lookup_cosmic_with_retry", lambda *args, **kw:
        DatabaseEvidence("COSMIC", "identity_mismatch", "wrong variant", raw={"failure_kind": "identity_mismatch"}))
    result = service._lookup_cosmic_variant(object(), variant(), tmp_path, progress=None)
    assert result.status == "manual_review"
    assert result.raw["failure_kind"] == "identity_mismatch"


def test_cosmic_cached_id_does_not_skip_identity_for_another_variant(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    calls = []
    def lookup(page, requested, *args, **kw):
        calls.append(requested.hgvsc)
        return DatabaseEvidence("COSMIC", "found", "synthetic")
    monkeypatch.setattr(service, "_lookup_cosmic_with_retry", lookup)
    first = variant()
    service._lookup_cosmic_variant(object(), first, tmp_path, progress=None)
    service._lookup_cosmic_variant(object(), replace(first, hgvsc="NM_000546.6:c.524G>T"), tmp_path, progress=None)
    assert len(calls) == 2


def mtbp_row(change):
    return {"gene": "TP53", "identity_text": change, "alteration": change,
        "section": "Reported variants", "functional_evidence": "", "biomarkers": "", "source_links": []}


@pytest.mark.parametrize("protein", ["p.?", "p.(?)", "p.=", "-"])
def test_mtbp_unknown_protein_does_not_block_exact_cdna_match(protein):
    requested = replace(variant(), hgvsp=protein)
    result = parse_mtbp_report("", [mtbp_row("c.524G>A")], [requested], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(requested)].status == "found"
    assert _mtbp_screenshot_row_matches("TP53 c.524G>A p.?", requested)


def test_mtbp_cdna_prefix_does_not_match_longer_change():
    requested = replace(variant(), hgvsp="", hgvsc="NM_000546.6:c.100del")
    result = parse_mtbp_report("", [mtbp_row("c.100delinsA")], [requested], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(requested)].status == "not_found"


def test_oncokb_unknown_protein_cdna_route_still_verifies_identity():
    requested = replace(variant(), hgvsp="p.?", genomic_location="")
    result = parse_oncokb_page(
        "TP53 c.524G>A\nVariant Overview\nMutation Effect\nOncogenicity\nUnknown",
        requested, _oncokb_query_urls(requested)[0],
    )
    assert result.status == "found"


class LoadingPage:
    url = "https://www.oncokb.org/gene/TP53/somatic/R175H"

    def locator(self, selector):
        class Locator:
            def inner_text(self):
                return "Variant Overview\nLoading..."
        return Locator()

    def wait_for_timeout(self, milliseconds):
        pass


def test_oncokb_timeout_reports_missing_fields(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    with pytest.raises(TimeoutError) as error:
        service._wait_for_oncokb_result(LoadingPage())
    assert error.value.diagnostics["render_diagnostics"] == {
        "page_type": "variant", "variant_overview": True, "mutation_effect": False,
    }


def test_cosmic_missing_canonical_link_has_structured_diagnostics(tmp_path):
    class SearchPage(LoadingPage):
        url = "https://cancer.sanger.ac.uk/cosmic/search?q=COSM10648"

        def locator(self, selector):
            class Locator:
                def inner_text(self):
                    return "Loading..."

                def evaluate_all(self, script):
                    return []
            return Locator()

    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    with pytest.raises(ProviderLookupError) as error:
        service._resolve_cosmic_mutation_page(SearchPage(), variant())
    assert error.value.diagnostics["failure_stage"] == "canonical_link_resolution"
    assert error.value.diagnostics["render_diagnostics"]["result_links"] == 0


def test_cosmic_mismatch_is_retained_if_remaining_id_is_missing(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    calls = []
    def lookup(*args, **kwargs):
        calls.append(1)
        return DatabaseEvidence("COSMIC", "identity_mismatch" if len(calls) == 1 else "not_found", "synthetic")
    monkeypatch.setattr(service, "_lookup_cosmic_with_retry", lookup)
    result = service._lookup_cosmic_variant(object(), replace(variant(), cosmic_id="COSM111;COSM222"), tmp_path, progress=None)
    assert len(calls) == 2
    assert result.status == "manual_review"


def test_oncokb_two_render_failures_remain_error_with_bounded_attempts(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    monkeypatch.setattr(service, "_lookup_oncokb_url", lambda *args: DatabaseEvidence(
        "OncoKB", "error", "synthetic", raw={"failure_kind": "transient", "failure_stage": "result_rendering"}))
    result = service._lookup_oncokb_variant(object(), variant(), tmp_path)
    assert result.status == "error"
    assert len(result.raw["query_attempts"]) == 2
