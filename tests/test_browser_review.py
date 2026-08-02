from pathlib import Path

import pytest

from archer_processor.io import ArcherTsvReader
from archer_processor.core.models import VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewService,
    _mtbp_genomic_query,
    _mtbp_query_rejected,
    _mtbp_unmapped_queries,
    _mtbp_variant_query,
    parse_franklin_page,
    parse_mtbp_report,
    parse_oncokb_page,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


def test_browser_query_urls_use_normalized_variant_identity(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)

    assert service.query_url("OncoKB", variant) == (
        "https://www.oncokb.org/gene/TP53/somatic/R175H"
    )
    assert service.query_url("Franklin", variant) == (
        "https://franklin.genoox.com/clinical-db/home"
    )
    assert service.query_url("MTBP", variant) == "https://mtbp.org/analyse/"
    with pytest.raises(ValueError, match="Unsupported browser database"):
        service.login_url("HSMD")


def test_browser_sources_use_canonical_order_with_mtbp_last(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)
    visited = []

    def record(database, variants, artifact_directory, *, progress):
        visited.append(database)
        return {}

    monkeypatch.setattr(service, "_search_database", record)
    service.search_variants(
        [variant], ["MTBP", "Franklin", "OncoKB"], tmp_path / "audit"
    )

    assert visited == ["OncoKB", "Franklin", "MTBP"]


def test_oncokb_visible_page_parser_extracts_core_evidence():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    body = """
    BRAF V600E Somatic
    Variant Overview
    The BRAF V600E mutation is known to be oncogenic.
    Mutation Effect
    Oncogenicity
    Oncogenic
    Biological Effect
    Gain-of-function
    Highest Level of Evidence
    """

    evidence = parse_oncokb_page(body, variant, "https://www.oncokb.org/example")

    assert evidence.status == "found"
    assert evidence.clinical_significance == "Oncogenic"
    assert "mutation_effect=Gain-of-function" in evidence.summary
    assert "known to be oncogenic" in evidence.summary


def test_oncokb_login_accepts_successful_redirect_after_click_error(tmp_path):
    service = BrowserReviewService(
        profile_root=tmp_path,
        oncokb_email="researcher@example.org",
        oncokb_password="secret",
    )

    class Locator:
        def __init__(self, page, *, sign_in=False):
            self.page = page
            self.sign_in = sign_in

        def count(self):
            return 0

        def fill(self, value):
            return None

        def click(self):
            if self.sign_in:
                self.page.url = "https://www.oncokb.org/"
                raise RuntimeError("element detached during navigation")

    class Page:
        url = "https://www.oncokb.org/login"

        def locator(self, selector):
            return Locator(self)

        def get_by_role(self, role, name, exact=True):
            return Locator(self, sign_in=name == "Sign in")

    assert service._try_saved_login("OncoKB", Page())


def test_oncokb_waits_for_client_rendered_variant_content(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=2_000)

    class Body:
        calls = 0

        def inner_text(self):
            self.calls += 1
            if self.calls == 1:
                return "Loading"
            return "Variant Overview\nUnknown\nMutation Effect\nOncogenicity"

    class Page:
        url = "https://www.oncokb.org/gene/EZH2/somatic/c.118-5_118-4del"
        body = Body()

        def locator(self, selector):
            assert selector == "body"
            return self.body

        def wait_for_timeout(self, milliseconds):
            return None

    page = Page()
    service._wait_for_oncokb_result(page)
    assert page.body.calls == 2


def test_franklin_visible_page_parser_returns_only_classification():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    body = """
    TP53:c.524G>A
    Franklin ACMG Classification
    Suggested classification
    Pathogenic
    EVIDENCE
    Functional Data
    PS3
    Strong
    Population Data
    PM2
    Moderate
    unmet:
    BA1 | BS1
    """

    evidence = parse_franklin_page(body, variant, "https://franklin.genoox.com/example")

    assert evidence.status == "found"
    assert evidence.clinical_significance == "Pathogenic"
    assert evidence.summary == "classification=Pathogenic;"
    assert evidence.raw == {"classification": "Pathogenic"}


def test_franklin_visible_page_parser_rejects_wrong_variant_identity():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    body = """
    TP53:c.524G>T
    p.Arg175Leu
    Suggested classification
    Likely pathogenic
    """

    evidence = parse_franklin_page(body, variant, "https://franklin.genoox.com/example")

    assert evidence.status == "identity_mismatch"
    assert evidence.clinical_significance == ""


def test_mtbp_report_parser_maps_exact_variant_and_core_evidence():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    body = """
    Analysis run date: 07/31/2026 19:04
    Pipeline version: 7.6.4
    OncoKB: v7.4
    Human genome: GRCh37/hg19
    Cancer type: Blood
    """
    rows = [
        {
            "section": "Putative functionally relevant variants: 1",
            "gene": "TP53",
            "gene_info": "TS SF",
            "alteration": "Mutation\nmissense\np.Arg175His\nexon 5/11",
            "identity_text": "p.Arg175His ENST00000269305.4:c.524G>A",
            "functional_evidence": (
                "Evidence A (curated):\n>Pathogenic, ClinVar\n>Oncogenic, OncoKB"
            ),
            "biomarkers": (
                "Tier 3-Cancer repurposing, 4 assertions\n"
                "Tier 4-Hypothet. (basket match), 3 assertions"
            ),
            "source_links": [
                "https://www.ncbi.nlm.nih.gov/clinvar/variation/12374",
                "https://oncokb.org/#/gene/TP53/alteration/R175H",
            ],
        }
    ]

    results = parse_mtbp_report(
        body,
        rows,
        [variant],
        "https://mtbp.org/patients/1/sample/1/report/1/",
        cancer_type="Blood",
    )
    evidence = results[BrowserReviewService.variant_key(variant)]

    assert evidence.status == "found"
    assert evidence.clinical_significance == "Putative functionally relevant variants"
    assert "Evidence A (curated)" in evidence.summary
    assert evidence.raw["pipeline_version"] == "7.6.4"
    assert evidence.raw["database_versions"]["OncoKB"] == "v7.4"
    assert evidence.raw["cancer_type"] == "Blood"
    assert len(evidence.raw["actionability_tiers"]) == 2


def test_mtbp_report_parser_fails_closed_on_variant_identity_mismatch():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    rows = [{
        "section": "Putative functionally relevant variants: 1",
        "gene": "TP53",
        "alteration": "p.Arg248Gln",
        "identity_text": "p.Arg248Gln",
        "functional_evidence": "Evidence A (curated)",
        "biomarkers": "",
        "source_links": [],
    }]

    results = parse_mtbp_report("Pipeline version: 7.6.4", rows, [variant], "url", cancer_type="Blood")
    evidence = results[BrowserReviewService.variant_key(variant)]

    assert evidence.status == "not_found"
    assert evidence.raw["candidate_count"] == 0


def test_mtbp_report_parser_matches_protein_duplication_notation():
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC",
        symbol="CEBPA",
        hgvsc="NM_004364.4:c.584_589dup",
        hgvsp="NP_004355.2:p.His195_Pro196dup",
    )
    rows = [{
        "section": "Putative functionally neutral variants: 1",
        "gene": "CEBPA",
        "gene_info": "TS",
        "alteration": "Mutation\ninframe insertion\np.His195_Pro196dup\nexon 1/1",
        "identity_text": "Mutation inframe insertion p.His195_Pro196dup exon 1/1",
        "functional_evidence": "Evidence A (curated): Benign/Likely benign, ClinVar",
        "biomarkers": "Not contemplated",
        "source_links": [],
    }]

    results = parse_mtbp_report(
        "Pipeline version: 7.6.4", rows, [variant], "url", cancer_type="Blood"
    )
    evidence = results[BrowserReviewService.variant_key(variant)]

    assert evidence.status == "found"
    assert evidence.clinical_significance == "Putative functionally neutral variants"


def test_mtbp_queries_prefer_transcript_qualified_hgvs():
    ezh2 = ArcherTsvReader().read(FIXTURE)[4]
    cebpa = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC",
        symbol="CEBPA",
        hgvsc="NM_004364.4:c.584_589dup",
        hgvsp="NP_004355.2:p.His195_Pro196dup",
        transcript="NM_004364.4",
    )

    assert _mtbp_variant_query(ezh2) == "NM_004456.4:c.118-4dup"
    assert _mtbp_variant_query(cebpa) == "NM_004364.4:c.584_589dup"


def test_mtbp_genomic_queries_convert_vcf_style_indels_to_hgvs():
    cebpa = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC",
        symbol="CEBPA",
        hgvsc="NM_004364.4:c.584_589dup",
        genomic_location="chr19:33792731",
        ref_allele="G",
        alt_allele="GGCGGGT",
    )
    ezh2 = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=2,
        sample="SYNTHETIC",
        symbol="EZH2",
        hgvsc="NM_004456.4:c.118-4dup",
        genomic_location="chr7:148543693",
        ref_allele="T",
        alt_allele="TA",
    )

    assert _mtbp_genomic_query(cebpa) == "chr19:g.33792731_33792732insGCGGGT"
    assert _mtbp_genomic_query(ezh2) == "chr7:g.148543693_148543694insA"


def test_mtbp_validation_error_identifies_rejected_entries():
    body = """
    The following mutation(s) cannot be mapped to genomic coordinates:

    CEBPA:p.His195_Pro196dup, EZH2:c.118-4dup

    Please check that the variant is passed following format specifications
    """

    rejected = _mtbp_unmapped_queries(body)

    assert rejected == ["CEBPA:p.His195_Pro196dup", "EZH2:c.118-4dup"]
    assert _mtbp_query_rejected("NM_004456.4:c.118-4dup", rejected)
    assert not _mtbp_query_rejected("NM_000546.6:c.524G>A", rejected)
