from dataclasses import replace
from pathlib import Path

import pytest

from archer_processor.core.models import VariantRecord
from archer_processor.services.browser_review import BrowserReviewService, parse_mtbp_report
from archer_processor.services.browser_review import parse_oncokb_page
from archer_processor.services.browser_review import _mtbp_screenshot_row_matches
from archer_processor.services import browser_popups
from archer_processor.services.provider_failures import ProviderLookupError, ProviderFailureKind


def variant(protein=""):
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1, sample="SYNTHETIC",
        symbol="TESTGENE", hgvsc="NM_999999.1:c.100del", hgvsp=protein,
        genomic_location="chr17:1000", ref_allele="AC", alt_allele="A",
    )


class SearchPage:
    url = "https://www.ncbi.nlm.nih.gov/clinvar/search/?q=synthetic"

    def __init__(self, text):
        self.text = text
        self.waits = []
        self.visited = []

    def locator(self, selector):
        page = self
        class Locator:
            def inner_text(self, **kwargs):
                return page.text

            def evaluate_all(self, script):
                return []
        return Locator()

    def goto(self, url, **kwargs):
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_current_clinvar_zero_variants_page_completes_and_uses_fallbacks(tmp_path):
    page = SearchPage(
        "Search results: 0 variants for synthetic\n"
        "Variation Gene Type Condition\nNo data available in table"
    )
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    result = service._lookup_clinvar_variant(page, variant(), tmp_path)
    assert result.status == "not_found"
    assert len(result.raw["query_attempts"]) == 4
    assert not page.waits


def test_empty_clinvar_table_alone_is_not_proof_of_absence(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    result = service._lookup_clinvar_variant(SearchPage("Loading... No data available in table"), variant(), tmp_path)
    assert result.status == "error"
    assert len(result.raw["query_attempts"]) == 1


def row(identity):
    return {"gene": "TESTGENE", "identity_text": identity, "alteration": identity,
        "section": "Reported variants", "functional_evidence": "", "biomarkers": "", "source_links": []}


@pytest.mark.parametrize("requested,reported", [
    ("p.Arg100SerfsTer5", "p.R100Sfs*5"),
    ("p.His100_Pro101dup", "p.H100_P101dup"),
    ("p.Asn100=", "p.N100="),
])
def test_mtbp_equivalent_long_and_short_protein_notation_matches(requested, reported):
    v = variant(requested)
    result = parse_mtbp_report("", [row(reported)], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "found"


def test_mtbp_different_frameshift_stop_is_rejected():
    v = variant("p.Arg100SerfsTer5")
    result = parse_mtbp_report("", [row("p.R100Sfs*6")], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"


def test_mtbp_missing_row_logs_report_and_gene_counts(tmp_path):
    v = variant()
    result = parse_mtbp_report("", [row("c.200del")], [v], "synthetic", cancer_type="Blood")
    evidence = result[BrowserReviewService.variant_key(v)]
    assert evidence.raw["report_row_count"] == 1
    messages = []
    BrowserReviewService(profile_root=tmp_path)._report_provider_results(
        "MTBP", [v], {BrowserReviewService.variant_key(v): evidence},
        duration_seconds=1, progress=messages.append,
    )
    message = next(m for m in messages if m.startswith("RESULT |"))
    assert "report_rows=1" in message and "gene_rows=1" in message
    assert "match_basis=cdna" in message


class OnboardingPage:
    def __init__(self, text, labels):
        self.text = text
        self.labels = labels
        self.clicked = []

    def locator(self, selector):
        page = self
        class Locator:
            def count(self): return len(page.labels) if selector == "button" else 1
            def nth(self, index): self.index = index; return self
            def is_visible(self): return True
            def inner_text(self, **kwargs): return page.text
            def all_inner_texts(self): return page.labels
            def locator(self, child): return page.locator(child)
            def click(self): page.clicked.append(page.labels[self.index])
        return Locator()

    def wait_for_timeout(self, ms): pass


def test_franklin_skips_optional_material_dialog_without_choosing_material():
    page = OnboardingPage("Choose sample material and tissue for your first analysis", ["Blood", "Tumor", "Skip"])
    assert browser_popups.dismiss_franklin_onboarding(page) == ["Skip"]
    assert page.clicked == ["Skip"]


def test_franklin_does_not_skip_unrelated_confirmation_dialog():
    page = OnboardingPage("Delete this report?", ["Skip", "Delete"])
    assert browser_popups.dismiss_franklin_onboarding(page) == []
    assert not page.clicked


def test_oncokb_login_prompt_on_variant_route_is_not_render_timeout(tmp_path):
    page = SearchPage("Please log in to view this variant")
    page.url = "https://www.oncokb.org/gene/TESTGENE/somatic/R100S"
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    with pytest.raises(ProviderLookupError) as error:
        service._wait_for_oncokb_result(page)
    assert error.value.kind == ProviderFailureKind.LOGIN_REQUIRED
    assert not page.waits


def test_oncokb_explicit_missing_variant_is_a_completed_result(tmp_path):
    page = SearchPage("GRCh37\n17:g.1001del\nWe do not have any information for this variant")
    page.url = "https://www.oncokb.org/hgvsg/17:g.1001del?refGenome=GRCh37"
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    service._wait_for_oncokb_result(page)
    result = parse_oncokb_page(page.text, variant(), page.url)
    assert result.status == "not_found"
    assert result.raw["failure_kind"] == "not_found"
    assert not page.waits


def test_mtbp_exact_genomic_identity_recovers_missing_cdna():
    v = variant()
    result = parse_mtbp_report("", [row("chr17:g.1001del (GRCh37)")], [v], "synthetic", cancer_type="Blood")
    evidence = result[BrowserReviewService.variant_key(v)]
    assert evidence.status == "found"
    assert evidence.raw["match_basis"] == "genomic_grch37"


@pytest.mark.parametrize("reported", [
    "chr17:g.1002del (GRCh37)", "chr17:g.1001del (GRCh38)",
    "chr17:g.1001del", "chr17:g.1001delinsT (GRCh37)",
])
def test_mtbp_genomic_recovery_rejects_unverified_or_different_identity(reported):
    v = variant()
    result = parse_mtbp_report("", [row(reported)], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"


def test_franklin_retries_assessment_panel_timeout_once_without_new_search(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    page = SearchPage("")
    page.url = "https://franklin.genoox.com/clinical-db/variant/snpTumor/synthetic"
    calls = []
    def capture(*args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("Edge element did not reach state 'visible'.")
        return [{"label": "Predictions", "path": "synthetic.png"}]
    monkeypatch.setattr(service, "_capture_franklin_assessment", capture)
    result = service._capture_franklin_assessment_with_retry(page, variant(), tmp_path)
    assert len(calls) == 2 and result[0]["label"] == "Predictions"
    assert not page.visited


def test_franklin_does_not_retry_ambiguous_assessment_panels(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path)
    calls = []
    def capture(*args):
        calls.append(1)
        raise ValueError("Franklin prediction/population evidence panels were ambiguous.")
    monkeypatch.setattr(service, "_capture_franklin_assessment", capture)
    with pytest.raises(ValueError):
        service._capture_franklin_assessment_with_retry(SearchPage(""), variant(), tmp_path)
    assert calls == [1]


@pytest.mark.parametrize("resolved", [True, False])
def test_clinvar_multiple_protein_hits_use_precise_fallback_before_manual_review(tmp_path, monkeypatch, resolved):
    requested = replace(variant("p.Arg100Ser"), hgvsc="NM_999999.1:c.100A>T", ref_allele="A", alt_allele="T")
    class AmbiguousPage(SearchPage):
        def goto(self, url, **kwargs):
            self.visited.append(url)
            if resolved and "NM_999999" in url:
                self.url = "https://www.ncbi.nlm.nih.gov/clinvar/variation/999999/"
                self.text = "NM_999999.1(TESTGENE):c.100A>T\nVariation ID: 999999\n17: 1000 (GRCh37)"
            else:
                self.url = url
                self.text = "Search results"

        def locator(self, selector):
            original = super().locator(selector)
            class Locator:
                def inner_text(self, **kw): return original.inner_text(**kw)
                def evaluate_all(self, script): return [
                    {"href": f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{identifier}/",
                     "text": "TESTGENE c.100A>T (p.Arg100Ser)"}
                    for identifier in (999998, 999999)
                ]
            return Locator()
    page = AmbiguousPage("")
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    monkeypatch.setattr(service, "_capture_clinvar_result", lambda v, e, p, d: e)
    result = service._lookup_clinvar_variant(page, requested, tmp_path)
    assert result.status == ("found" if resolved else "manual_review")
    assert len(result.raw["query_attempts"]) >= 2


def test_cosmic_final_link_timeout_saves_diagnostic_page_not_success_evidence(tmp_path, monkeypatch):
    from PIL import Image
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    class DiagnosticPage(SearchPage):
        def screenshot(self, *, path, full_page):
            Image.new("RGB", (80, 60), "white").save(path)
    page = DiagnosticPage("Synthetic canonical link did not load")
    def resolve(*args):
        raise ProviderLookupError(ProviderFailureKind.TRANSIENT, "No canonical link")
    monkeypatch.setattr(service, "_resolve_cosmic_mutation_page", resolve)
    result = service._lookup_cosmic_with_retry(page, variant(), "https://cancer.sanger.ac.uk/cosmic/search?q=COSM999999", tmp_path, progress=None)
    assert result.status == "transient"
    assert Path(result.raw["diagnostic_screenshot"]).exists()
    assert result.raw["visible_text_preview"] == page.text
    assert "screenshots" not in result.raw


def test_mtbp_declared_nonempty_report_with_no_extracted_rows_is_layout_error():
    v = variant()
    result = parse_mtbp_report("Putative functionally relevant variants: 2", [], [v], "synthetic", cancer_type="Blood")
    evidence = result[BrowserReviewService.variant_key(v)]
    assert evidence.status == "layout_changed"
    assert evidence.raw["failure_stage"] == "report_row_extraction"


def test_mtbp_capture_rejects_a_gene_with_matching_name_prefix():
    assert not _mtbp_screenshot_row_matches("TESTGENE2 c.100del", variant())


def test_hidden_cdna_from_different_refseq_transcript_does_not_verify_variant():
    v = variant()
    report_identity = "NM_888888.1:c.100del"
    result = parse_mtbp_report("", [row(report_identity)], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"
    assert not _mtbp_screenshot_row_matches("TESTGENE " + report_identity, v)


def test_hidden_cdna_exact_refseq_transcript_with_new_version_is_accepted():
    v = variant()
    result = parse_mtbp_report("", [row("NM_999999.2:c.100del")], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "found"


def test_known_protein_conflict_is_not_overridden_by_hidden_matching_cdna():
    v = variant("p.Arg100Ser")
    result = parse_mtbp_report("", [row("p.Arg100Gly NM_999999.1:c.100del")], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"


@pytest.mark.parametrize("protein", ["", "p.Arg100Ser"])
def test_reported_genomic_conflict_overrides_matching_cdna_or_protein(protein):
    v = variant(protein)
    report_identity = "NM_999999.1:c.100del p.Arg100Ser chr17:g.2001del (GRCh37)"
    result = parse_mtbp_report("", [row(report_identity)], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"
    assert not _mtbp_screenshot_row_matches("TESTGENE " + report_identity, v)


def test_shared_gene_cell_capture_is_marked_as_gene_context(tmp_path):
    import json
    from PIL import Image
    from archer_processor.services.capture_validation import CaptureValidation
    report = tmp_path / "synthetic-report.png"
    Image.new("RGB", (200, 100), "white").save(report)
    report.with_suffix(".geometry.json").write_text(json.dumps({
        "width": 200, "height": 100, "rows": [{
            "gene": "TESTGENE", "identity": "NM_999999.1:c.100del",
            "row": {"x": 0, "y": 20, "width": 200, "height": 60},
            "shared_gene_context": True,
        }],
    }))
    service = BrowserReviewService(profile_root=tmp_path, capture_validator=lambda p: CaptureValidation(True, "ok", 200, 60, 1))
    output = service._crop_mtbp_variant_from_report(None, variant(), tmp_path, report)
    with Image.open(output) as image:
        assert image.info["match_scope"] == "gene_context"


def test_separate_transcript_field_is_used_for_hidden_cdna_verification():
    v = replace(variant(), hgvsc="c.100del", transcript="NM_999999.1")
    report_identity = "NM_888888.1:c.100del"
    result = parse_mtbp_report("", [row(report_identity)], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"
    assert not _mtbp_screenshot_row_matches("TESTGENE " + report_identity, v)


@pytest.mark.parametrize("accession,other", [
    ("XM_999999.1", "XM_888888.1"), ("XR_999999.1", "XR_888888.1"),
    ("ENST999999.1", "ENST888888.1"),
])
def test_other_supported_transcript_families_require_matching_accession(accession, other):
    v = replace(variant(), hgvsc=f"{accession}:c.100del")
    result = parse_mtbp_report("", [row(f"{other}:c.100del")], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"


def test_unknown_input_transcript_does_not_accept_qualified_hidden_cdna():
    v = replace(variant(), hgvsc="c.100del", transcript="")
    result = parse_mtbp_report("", [row("NM_888888.1:c.100del")], [v], "synthetic", cancer_type="Blood")
    assert result[BrowserReviewService.variant_key(v)].status == "not_found"
