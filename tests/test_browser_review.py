from pathlib import Path

import pytest

from archer_processor.io import ArcherTsvReader
from archer_processor.services.browser_review import (
    BrowserReviewService,
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
        "https://franklin.genoox.com/clinical-db/variant/snp/chr17-7578406-G-A"
    )
    assert service.query_url("MTBP", variant) == "https://mtbp.org/analyse/"
    with pytest.raises(ValueError, match="Unsupported browser database"):
        service.login_url("HSMD")


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


def test_franklin_visible_page_parser_extracts_classification_and_rules():
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
    assert evidence.raw["displayed_acmg_rules"] == ["PS3", "PM2", "BA1", "BS1"]


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
