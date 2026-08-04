from pathlib import Path

import pytest

from archer_processor.io import ArcherTsvReader
from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewService,
    FRANKLIN_ASSESSMENT_RENDER_BUFFER_MS,
    _cosmic_identifier,
    _cosmic_numeric_id,
    _mtbp_genomic_query,
    _mtbp_query_rejected,
    _mtbp_screenshot_row_matches,
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
    assert service.query_url("ClinVar", variant).startswith(
        "https://www.ncbi.nlm.nih.gov/clinvar/?term="
    )
    assert service.query_url("COSMIC", variant) == (
        "https://cancer.sanger.ac.uk/cosmic/mutation/overview?id=10648"
    )
    with pytest.raises(ValueError, match="Unsupported browser database"):
        service.login_url("HSMD")


def test_browser_sources_use_canonical_order_with_mtbp_last(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
    )
    visited = []

    def record(database, variants, artifact_directory, *, progress):
        visited.append(database)
        return {}

    monkeypatch.setattr(service, "_search_database", record)
    service.search_variants(
        [variant], ["MTBP", "Franklin", "OncoKB", "COSMIC"], tmp_path / "audit"
    )

    assert visited == ["COSMIC", "OncoKB", "Franklin", "MTBP"]


def test_cosmic_identifier_uses_first_archer_cosmic_id():
    assert _cosmic_identifier("COSM476; COSV56056643") == "COSM476"
    assert _cosmic_numeric_id("COSM476; COSV56056643") == "476"
    assert _cosmic_numeric_id("") == ""


def test_browser_safety_buffer_randomizes_queries_and_provider_switches(
    tmp_path, monkeypatch
):
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=15_000,
        request_delay_max_ms=30_000,
    )
    random_bounds = []
    monkeypatch.setattr(
        "archer_processor.services.browser_review.random.randint",
        lambda lower, upper: random_bounds.append((lower, upper)) or 23_400,
    )
    slept = []
    monkeypatch.setattr(
        "archer_processor.services.browser_review.time.sleep",
        lambda seconds: slept.append(seconds),
    )

    class Page:
        waits = []

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()
    progress = []
    service._wait_between_queries(page, "OncoKB", progress=progress.append)
    service._wait_between_databases(
        "OncoKB", "Franklin", progress=progress.append
    )

    assert random_bounds == [(15_000, 30_000), (15_000, 30_000)]
    assert page.waits == [23_400]
    assert slept == [23.4]
    assert "23.4s before next variant" in progress[0]
    assert "23.4s between OncoKB and Franklin" in progress[1]


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


def test_clinvar_capture_is_cropped_to_title_and_classification_summary(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)
    api_evidence = DatabaseEvidence(
        "ClinVar",
        "found",
        "TP53 variant | Significance: Pathogenic",
        accession="VCV000012345.1",
        clinical_significance="Pathogenic",
        url="https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/",
        raw={"clinvar_id": "12345"},
    )

    class Locator:
        def __init__(self, box, text=""):
            self.box = box
            self.text = text

        def wait_for(self, **kwargs):
            pass

        def bounding_box(self):
            return self.box

        def inner_text(self):
            return self.text

    class Page:
        url = "https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/"

        def __init__(self):
            self.capture = None

        def locator(self, selector):
            if selector == "main div.functions-container":
                return Locator({"x": 100, "y": 200, "width": 1000, "height": 80})
            assert selector == "#germline-somatic-info"
            return Locator(
                {"x": 120, "y": 280, "width": 960, "height": 330},
                "Germline\nClassification\nPathogenic\nSomatic\nNo data submitted",
            )

        def screenshot(self, **kwargs):
            self.capture = kwargs

    page = Page()

    evidence = service._capture_clinvar_result(
        variant, api_evidence, page, tmp_path
    )

    assert evidence.status == "found"
    assert evidence.clinical_significance == "Pathogenic"
    assert evidence.raw["screenshots"][0]["label"] == "Classification summary"
    assert page.capture["clip"] == {
        "x": 100,
        "y": 200,
        "width": 1000,
        "height": 410,
    }


def test_franklin_primary_capture_uses_full_page(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)

    class Body:
        def inner_text(self, timeout):
            return """
            TP53:c.524G>A
            Suggested classification
            Pathogenic
            """

    class Page:
        url = "https://franklin.genoox.com/clinical-db/variant/snp/example"

        def __init__(self):
            self.capture = None

        def locator(self, selector):
            if selector == "body":
                return Body()

            class MissingPanel:
                def count(self):
                    return 0

            assert selector in {
                "gnx-mini-app-header img.close-btn",
                "gnx-result-page",
            }
            return MissingPanel()

        def screenshot(self, **kwargs):
            self.capture = kwargs

    page = Page()

    evidence = service._capture_result("Franklin", variant, page, tmp_path)

    assert evidence.status == "found"
    assert page.capture["full_page"] is True
    assert evidence.raw["screenshots"][0]["label"] == (
        "Full computed-classification page"
    )


def test_franklin_assessment_waits_for_dynamic_panels_before_capture(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)

    class Heading:
        def __init__(self, name):
            self.name = name

        def wait_for(self, **kwargs):
            pass

        def evaluate(self, script):
            pass

    class Section:
        def __init__(self, heading):
            self.heading = heading

        def count(self):
            return 1

        def bounding_box(self):
            top = 100 if self.heading.name == "Predictions" else 300
            return {"x": 20, "y": top, "width": 900, "height": 180}

    class Sections:
        def filter(self, *, has):
            return Section(has)

    class Page:
        url = "https://franklin.genoox.com/clinical-db/variant/snp/example"

        def __init__(self):
            self.waits = []
            self.capture = None

        def goto(self, url, **kwargs):
            self.url = url

        def get_by_role(self, role, *, name, exact):
            return Heading(name)

        def locator(self, selector):
            assert selector == "div.section"
            return Sections()

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

        def evaluate(self, script):
            pass

        def screenshot(self, **kwargs):
            self.capture = kwargs

    page = Page()

    screenshot = service._capture_franklin_assessment(page, variant, tmp_path)

    assert page.waits == [FRANKLIN_ASSESSMENT_RENDER_BUFFER_MS]
    assert FRANKLIN_ASSESSMENT_RENDER_BUFFER_MS == 5_000
    assert page.capture is not None
    assert screenshot["label"] == "Predictions and population frequencies"


def test_franklin_classification_capture_scrolls_complete_panel(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)

    class Panel:
        def __init__(self):
            self.positions = []

        def count(self):
            return 1

        def wait_for(self, **kwargs):
            pass

        def evaluate(self, script, argument=None):
            if "clientHeight" in script:
                return {"clientHeight": 400, "scrollHeight": 1000}
            actual = 0 if argument is None else min(argument, 600)
            self.positions.append(actual)
            return actual

        def bounding_box(self):
            return {"x": 10, "y": 200, "width": 1200, "height": 400}

    panel = Panel()

    class DeNovo:
        def count(self):
            return 1

        def evaluate(self, script):
            assert "category-box" in script
            return 900

    class Page:
        url = "https://franklin.genoox.com/clinical-db/variant/snp/example"

        def __init__(self):
            self.captures = []

        def locator(self, selector):
            if selector == "gnx-result-page":
                return panel

            class MissingCloseButton:
                def count(self):
                    return 0

            assert selector == "gnx-mini-app-header img.close-btn"
            return MissingCloseButton()

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 200

        def get_by_text(self, text, *, exact):
            assert text == "De Novo Data"
            assert exact
            return DeNovo()

        def screenshot(self, **kwargs):
            self.captures.append(kwargs)

    page = Page()
    screenshots = service._capture_franklin_classification(
        page, variant, tmp_path
    )

    assert len(screenshots) == 3
    assert panel.positions == [0, 400, 600, 0]
    assert screenshots[0]["label"].endswith("(1 of 3)")
    assert screenshots[-1]["label"].endswith("(3 of 3)")
    assert [capture["clip"]["height"] for capture in page.captures] == [
        400,
        400,
        100,
    ]
    assert page.captures[-1]["clip"]["y"] == 400


def test_franklin_search_explicitly_selects_hg19_and_somatic(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    selected = []

    class Option:
        def __init__(self, name):
            self.name = name

        def count(self):
            return 1

        def wait_for(self, **kwargs):
            pass

        def click(self):
            selected.append(self.name)

    class Combo:
        def click(self):
            pass

    class Combos:
        def count(self):
            return 2

        def nth(self, index):
            return Combo()

    class Page:
        def get_by_role(self, role, **kwargs):
            if role == "combobox":
                return Combos()
            assert role == "option"
            assert kwargs["exact"]
            return Option(kwargs["name"])

        def wait_for_timeout(self, milliseconds):
            pass

    service._select_franklin_search_mode(Page())

    assert selected == ["hg19", "Somatic"]


def test_franklin_resolver_accepts_somatic_variant_route(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    variant = ArcherTsvReader().read(FIXTURE)[3]

    class Page:
        url = (
            "https://franklin.genoox.com/clinical-db/variant/"
            "snpTumor/chr17-7578406-C-T"
        )

    service._open_franklin_resolved_variant(Page(), variant)


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


def test_mtbp_screenshot_row_matching_requires_exact_variant_identity():
    variant = ArcherTsvReader().read(FIXTURE)[3]

    assert _mtbp_screenshot_row_matches(
        "TP53 TS Mutation missense p.Arg175His exon 5/11", variant
    )
    assert not _mtbp_screenshot_row_matches(
        "TP53 TS Mutation missense p.Arg248Gln exon 7/11", variant
    )
    assert not _mtbp_screenshot_row_matches(
        "EZH2 TS Mutation missense p.Arg175His exon 5/11", variant
    )


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


def test_mtbp_report_parser_ignores_warning_marker_in_gene_cell():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    rows = [{
        "section": "Putative functionally relevant variants: 1",
        "gene": "TP53\n!!",
        "alteration": "Mutation\nmissense\np.Arg175His\nexon 5/11",
        "identity_text": "p.Arg175His ENST00000269305.4:c.524G>A",
        "functional_evidence": "Evidence A (curated)",
        "biomarkers": "",
        "source_links": [],
    }]

    results = parse_mtbp_report(
        "Pipeline version: 7.6.4", rows, [variant], "url", cancer_type="Blood"
    )
    evidence = results[BrowserReviewService.variant_key(variant)]

    assert evidence.status == "found"
    assert evidence.raw["match_basis"] == "protein"


def test_mtbp_report_parser_accepts_unique_gene_row_without_reported_identity():
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC",
        symbol="KDM6A",
        hgvsc="NM_001291415.1:c.-194T>G",
        hgvsp="",
    )
    rows = [{
        "section": "Putative functionally relevant variants: 1",
        "gene": "KDM6A",
        "alteration": "Mutation\nupstream gene\n-",
        "identity_text": "Mutation upstream gene -",
        "functional_evidence": "Evidence B (curated)",
        "biomarkers": "",
        "source_links": [],
    }]

    results = parse_mtbp_report(
        "Pipeline version: 7.6.4", rows, [variant], "url", cancer_type="Blood"
    )
    evidence = results[BrowserReviewService.variant_key(variant)]

    assert evidence.status == "found"
    assert evidence.raw["match_basis"] == "unique_gene_without_reported_identity"


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


def test_mtbp_reuses_proven_genomic_fallback_after_transcript_rejection(tmp_path):
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="26OUM10892_VPM_S30_R1_001",
        symbol="ASXL1",
        hgvsc="NM_015338.5:c.1854dup",
        hgvsp="NP_056153.2:p.Ala619SerfsTer16",
        genomic_location="chr20:31022366",
        ref_allele="G",
        alt_allele="GA",
    )
    service = BrowserReviewService(profile_root=tmp_path)

    initial_query, attempts, learned = service._mtbp_initial_query(variant)
    assert initial_query == "NM_015338.5:c.1854dup"
    assert attempts == ["NM_015338.5:c.1854dup"]
    assert not learned

    service._mtbp_rejected_transcript_queries.add(initial_query)
    retry_query, retry_attempts, learned = service._mtbp_initial_query(variant)

    assert retry_query == "chr20:g.31022366_31022367insA"
    assert retry_attempts == [
        "NM_015338.5:c.1854dup",
        "chr20:g.31022366_31022367insA",
    ]
    assert learned


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


def test_mtbp_queue_recovers_report_from_reports_list(tmp_path):
    service = BrowserReviewService(
        profile_root=tmp_path,
        navigation_timeout_ms=100,
        analysis_timeout_ms=1_000,
    )
    analysis_id = "ARCHER-20260804T120000Z-test"

    class FakeBrowserTimeout(Exception):
        pass

    class ReportLink:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1

        def click(self):
            self.page.url = "https://mtbp.org/patients/1/sample/1/report/2/"

    class Page:
        def __init__(self):
            self.url = "https://mtbp.org/queue/123/"
            self.gotos = []

        def wait_for_url(self, pattern, *, timeout):
            if pattern.fullmatch(self.url):
                return
            raise FakeBrowserTimeout()

        def goto(self, url, **kwargs):
            self.url = url
            self.gotos.append(url)

        def get_by_role(self, role, *, name, exact):
            assert role == "link"
            assert name == analysis_id
            assert exact
            return ReportLink(self)

        def wait_for_timeout(self, milliseconds):
            pass

    page = Page()
    service._wait_for_mtbp_report(
        page,
        analysis_id,
        FakeBrowserTimeout,
        progress=None,
    )

    assert page.url.endswith("/report/2/")
    assert page.gotos == ["https://mtbp.org/patients/"]


class _FakeMtbpDialog:
    type = "confirm"

    def __init__(self):
        self.accepted = False

    def accept(self):
        self.accepted = True

    def dismiss(self):
        self.accepted = False


class _FakeMtbpReportLink:
    def __init__(self, page, analysis_id):
        self.page = page
        self.analysis_id = analysis_id

    def count(self):
        return int(self.analysis_id in self.page.reports)

    def locator(self, selector):
        assert selector == "xpath=ancestor::tr[1]"
        return _FakeMtbpRow(self.page, self.analysis_id)

    def wait_for(self, *, state, timeout):
        assert state == "detached"
        assert self.analysis_id not in self.page.reports


class _FakeMtbpRow:
    def __init__(self, page, analysis_id):
        self.page = page
        self.analysis_id = analysis_id

    def locator(self, selector):
        assert selector == "button.delete-patient"
        return _FakeMtbpDeleteButton(self.page, self.analysis_id)


class _FakeMtbpDeleteButton:
    def __init__(self, page, analysis_id):
        self.page = page
        self.analysis_id = analysis_id

    def count(self):
        return int(self.analysis_id in self.page.reports)

    def get_attribute(self, name):
        assert name == "data-patient-name"
        return self.analysis_id

    def click(self):
        dialog = _FakeMtbpDialog()
        self.page.dialog_handler(dialog)
        self.page.dialogs.append(dialog)
        if dialog.accepted:
            self.page.reports.remove(self.analysis_id)


class _FakeMtbpGeneratedButtons:
    def __init__(self, page):
        self.page = page

    def count(self):
        return len([name for name in self.page.reports if name.startswith("ARCHER-")])

    def nth(self, index):
        analysis_id = [
            name for name in self.page.reports if name.startswith("ARCHER-")
        ][index]
        return _FakeMtbpDeleteButton(self.page, analysis_id)


class _FakeMtbpAllDeleteButtons:
    def __init__(self, page):
        self.page = page

    def count(self):
        return len(self.page.reports)


class _FakeMtbpReportsPage:
    def __init__(self, reports):
        self.reports = list(reports)
        self.url = "https://mtbp.org/analyse/"
        self.dialog_handler = None
        self.dialogs = []

    def goto(self, url, **kwargs):
        self.url = url

    def get_by_role(self, role, *, name, exact):
        assert role == "link"
        assert exact
        return _FakeMtbpReportLink(self, name)

    def locator(self, selector):
        if selector == "button.delete-patient[data-patient-name^='ARCHER-']":
            return _FakeMtbpGeneratedButtons(self)
        if selector == "button.delete-patient":
            return _FakeMtbpAllDeleteButtons(self)
        raise AssertionError(selector)

    def once(self, event, handler):
        assert event == "dialog"
        self.dialog_handler = handler


def test_mtbp_deletes_exact_generated_report_after_confirming(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = _FakeMtbpReportsPage(["manual-report", "ARCHER-20260802T120000Z-test"])

    outcome = service._delete_mtbp_report(
        page, "ARCHER-20260802T120000Z-test"
    )

    assert outcome["status"] == "deleted"
    assert page.reports == ["manual-report"]
    assert len(page.dialogs) == 1
    assert page.dialogs[0].accepted


def test_mtbp_capacity_cleanup_removes_all_archer_reports_when_full(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = _FakeMtbpReportsPage(
        [
            "new-manual",
            "ARCHER-newer",
            "manual-1",
            "manual-2",
            "manual-3",
            "manual-4",
            "manual-5",
            "manual-6",
            "manual-7",
            "ARCHER-oldest",
        ]
    )
    progress = []

    outcome = service._cleanup_stale_mtbp_reports(page, progress=progress.append)

    assert not any(name.startswith("ARCHER-") for name in page.reports)
    assert all(name.startswith("manual-") or name == "new-manual" for name in page.reports)
    assert outcome["status"] == "deleted_batch"
    assert outcome["trigger"] == "report_capacity"
    assert outcome["deleted_stale_reports"] == [
        "ARCHER-newer",
        "ARCHER-oldest",
    ]
    assert outcome["remaining_reports"] == 8
    assert outcome["remaining_archer_reports"] == 0
    assert len(progress) == 2


def test_mtbp_cleanup_deletes_all_six_archer_reports_as_one_batch(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    archer_reports = [f"ARCHER-run-{index}" for index in range(6)]
    page = _FakeMtbpReportsPage(["manual-1", *archer_reports, "manual-2"])

    outcome = service._cleanup_stale_mtbp_reports(page, progress=None)

    assert page.reports == ["manual-1", "manual-2"]
    assert outcome["status"] == "deleted_batch"
    assert outcome["trigger"] == "archer_threshold"
    assert outcome["deleted_stale_reports"] == archer_reports


def test_mtbp_cleanup_retains_fewer_than_six_archer_reports(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    reports = ["manual", *[f"ARCHER-run-{index}" for index in range(5)]]
    page = _FakeMtbpReportsPage(reports)

    outcome = service._cleanup_stale_mtbp_reports(page, progress=None)

    assert page.reports == reports
    assert outcome["status"] == "retained"
    assert outcome["remaining_archer_reports"] == 5


def test_mtbp_preflight_refuses_to_delete_ten_manual_reports(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = _FakeMtbpReportsPage([f"manual-{index}" for index in range(10)])

    with pytest.raises(RuntimeError, match="delete an older report manually"):
        service._cleanup_stale_mtbp_reports(page, progress=None)
