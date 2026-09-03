from pathlib import Path

import pytest
from PIL import Image

from archer_processor.io import ArcherTsvReader
from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewCancelled,
    BrowserReviewService,
    _cosmic_identifier,
    _cosmic_identifiers,
    _cosmic_numeric_id,
    _cosmic_source_url,
    _expanded_capture_box,
    _franklin_queries,
    _mtbp_genomic_query,
    _mtbp_query_rejected,
    _mtbp_retry_query,
    _mtbp_screenshot_row_matches,
    _mtbp_unmapped_queries,
    _mtbp_variant_query,
    parse_franklin_page,
    parse_mtbp_report,
    parse_oncokb_page,
)
from archer_processor.services.capture_validation import CaptureValidation


VALID_CAPTURE = lambda _: CaptureValidation(True, "ok", 800, 500, 10.0)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


def test_expanded_capture_box_adds_margins_and_clamps_to_document():
    assert _expanded_capture_box(
        {"x": 10, "y": 20, "width": 100, "height": 80},
        {"x": 0, "y": 0, "width": 500, "height": 400},
    ) == {"x": 0.0, "y": 0.0, "width": 142.0, "height": 124.0}
    assert _expanded_capture_box(
        {"x": 450, "y": 350, "width": 45, "height": 45},
        {"x": 0, "y": 0, "width": 500, "height": 400},
    ) == {"x": 418.0, "y": 326.0, "width": 82.0, "height": 74.0}


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
        "https://cancer.sanger.ac.uk/cosmic/search?q=COSM10648"
    )
    with pytest.raises(ValueError, match="Unsupported browser database"):
        service.login_url("HSMD")


def test_browser_sources_use_canonical_order_with_mtbp_last(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
        provider_switch_delay_ms=0,
    )
    visited = []

    def record(
        database, variants, artifact_directory, *, progress, prior_evidence=None
    ):
        visited.append(database)
        return {}

    monkeypatch.setattr(service, "_search_database", record)
    service.search_variants(
        [variant], ["MTBP", "Franklin", "OncoKB", "COSMIC"], tmp_path / "audit"
    )

    assert visited == ["COSMIC", "OncoKB", "Franklin", "MTBP"]


def test_browser_review_reports_provider_with_progress(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
        provider_switch_delay_ms=0,
    )
    seen = []

    def record(
        database, variants, artifact_directory, *, progress, prior_evidence=None
    ):
        return {
            service.variant_key(variant): DatabaseEvidence(
                database, "not_found", "synthetic"
            )
        }

    monkeypatch.setattr(service, "_search_database", record)
    service.search_variants(
        [variant],
        ["Franklin"],
        tmp_path / "audit",
        activity=lambda database, message: seen.append((database, message)),
    )

    assert seen[0][0] == "Franklin"
    assert "starting" in seen[0][1].casefold()


def test_browser_resume_skips_completed_sources_and_checkpoints_each_provider(
    tmp_path, monkeypatch
):
    variants = ArcherTsvReader().read(FIXTURE)[3:5]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
        provider_switch_delay_ms=0,
    )
    first_key = service.variant_key(variants[0])
    calls = []

    def record(
        database, pending, artifact_directory, *, progress, prior_evidence=None
    ):
        calls.append((database, [service.variant_key(variant) for variant in pending]))
        return {
            service.variant_key(variant): DatabaseEvidence(
                database, "found", "complete"
            )
            for variant in pending
        }

    monkeypatch.setattr(service, "_search_database", record)
    checkpoints = []

    results = service.search_variants(
        variants,
        ["COSMIC", "ClinVar"],
        tmp_path / "audit",
        completed_sources={(first_key, "COSMIC")},
        checkpoint=checkpoints.append,
    )

    assert calls == [
        ("COSMIC", [service.variant_key(variants[1])]),
        ("ClinVar", [service.variant_key(variant) for variant in variants]),
    ]
    assert len(checkpoints) == 2
    assert results[first_key][0].database == "ClinVar"


def test_cosmic_identifier_uses_first_archer_cosmic_id():
    assert _cosmic_identifier("COSM476; COSV56056643") == "COSM476"
    assert _cosmic_identifiers("COSM476; COSV56056643, COSM476") == [
        "COSM476",
        "COSV56056643",
    ]
    assert _cosmic_numeric_id("COSM476; COSV56056643") == "476"
    assert _cosmic_numeric_id("") == ""


def test_cosmic_lookup_tries_each_identifier_until_verified_match(
    tmp_path, monkeypatch
):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    variant.cosmic_id = "COSM111; COSV222"
    service = BrowserReviewService(profile_root=tmp_path)
    attempts = []

    def lookup(page, candidate, query_url, artifact_directory, *, progress):
        attempts.append(candidate.cosmic_id)
        return DatabaseEvidence(
            "COSMIC",
            "found" if candidate.cosmic_id == "COSV222" else "not_found",
            "matched" if candidate.cosmic_id == "COSV222" else "missing",
            accession=candidate.cosmic_id,
        )

    monkeypatch.setattr(service, "_lookup_cosmic_with_retry", lookup)

    evidence = service._lookup_cosmic_variant(
        object(), variant, tmp_path / "cosmic", progress=None
    )

    assert attempts == ["COSM111", "COSV222"]
    assert evidence.status == "found"
    assert evidence.accession == "COSV222"
    assert evidence.raw["query_attempts"] == ["COSM111", "COSV222"]


def test_cosmic_search_resolves_canonical_internal_mutation_id(tmp_path):
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="26OUM09411_VPM_S20_R1_001",
        symbol="ASXL1",
        hgvsc="NM_015338.5:c.1934dup",
        cosmic_id="COSM1411076",
    )
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    grch38 = (
        "https://cancer.sanger.ac.uk/cosmic/mutation/overview"
        "?id=139096009&merge=1411076"
    )
    grch37 = (
        "https://cancer.sanger.ac.uk/cosmic/mutation/overview"
        "?cosm=COSM34210&genome=37&id=43598117&trans=ASXL1"
    )

    class Links:
        def __init__(self, values):
            self.values = values

        def evaluate_all(self, script):
            return self.values

        def wait_for(self, *, state, timeout):
            assert state == "visible"

    class Page:
        url = "https://cancer.sanger.ac.uk/cosmic/search?q=COSM1411076"

        def locator(self, selector):
            if selector == "a[href*='/cosmic/mutation/overview']":
                return Links([grch38])
            if selector == "a[href*='genome=37'][href*='id=']":
                return Links([grch37])
            raise AssertionError(selector)

        def goto(self, url, **kwargs):
            self.url = url

        def wait_for_timeout(self, milliseconds):
            pass

    page = Page()
    service._resolve_cosmic_mutation_page(page, variant)

    assert page.url == grch37
    assert _cosmic_source_url(page.url, variant.cosmic_id) == grch37


def test_cosmic_lookup_retries_transient_render_failure(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=100)
    attempts = []

    class Page:
        url = "https://cancer.sanger.ac.uk/cosmic/search?q=COSM10648"

        def goto(self, url, **kwargs):
            self.url = url
            attempts.append(url)

        def wait_for_timeout(self, milliseconds):
            pass

    page = Page()
    waits = 0

    monkeypatch.setattr(service, "_resolve_cosmic_mutation_page", lambda page, variant: None)

    def wait_for_result(page):
        nonlocal waits
        waits += 1
        if waits == 1:
            raise TimeoutError("transient render")

    monkeypatch.setattr(service, "_wait_for_cosmic_result", wait_for_result)
    monkeypatch.setattr(
        service,
        "_capture_cosmic_result",
        lambda variant, page, directory: DatabaseEvidence("COSMIC", "found", "ok"),
    )

    evidence = service._lookup_cosmic_with_retry(
        page,
        variant,
        service.query_url("COSMIC", variant),
        tmp_path / "cosmic",
        progress=None,
    )

    assert evidence.status == "found"
    assert len(attempts) == 2


def test_cosmic_lookup_does_not_retry_user_cancellation(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)
    gotos = []

    class Page:
        url = ""

        def goto(self, url, **kwargs):
            gotos.append(url)

        def wait_for_timeout(self, milliseconds):
            pass

    monkeypatch.setattr(
        service,
        "_resolve_cosmic_mutation_page",
        lambda page, variant: (_ for _ in ()).throw(BrowserReviewCancelled()),
    )

    with pytest.raises(BrowserReviewCancelled):
        service._lookup_cosmic_with_retry(
            Page(),
            variant,
            service.query_url("COSMIC", variant),
            tmp_path,
            progress=None,
        )

    assert len(gotos) == 1


def test_cosmic_ready_check_uses_sections_instead_of_duplicate_headings(
    tmp_path, monkeypatch
):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=100)

    class Rows:
        def count(self):
            return 1

    class Section:
        def locator(self, selector):
            assert selector == "tbody tr"
            return Rows()

    monkeypatch.setattr(service, "_cosmic_section", lambda page, heading: Section())

    class Page:
        def get_by_role(self, *args, **kwargs):
            raise AssertionError("duplicate responsive headings must not be awaited")

        def wait_for_timeout(self, milliseconds):
            pass

    service._wait_for_cosmic_result(Page())


def test_oncokb_rejects_cookie_banner_before_capture(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)

    class RejectButton:
        clicked = False

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.clicked = True

    class Overlays:
        removed = False

        def evaluate_all(self, script):
            self.removed = True

    class Page:
        def __init__(self):
            self.reject = RejectButton()
            self.overlays = Overlays()

        def get_by_role(self, role, *, name, exact):
            assert (role, name, exact) == ("button", "Reject all", True)
            return self.reject

        def locator(self, selector):
            return self.overlays

        def wait_for_timeout(self, milliseconds):
            pass

    page = Page()
    service._reject_oncokb_cookies(page)

    assert page.reject.clicked
    assert page.overlays.removed


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

    assert random_bounds == [(15_000, 30_000)]
    assert sum(page.waits) == 23_400
    assert all(wait <= 250 for wait in page.waits)
    assert sum(slept) == pytest.approx(3.0)
    assert all(wait <= 0.25 for wait in slept)
    assert "23.4s before next variant" in progress[0]
    assert "3.0s between OncoKB and Franklin" in progress[1]


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


def test_franklin_login_waits_for_saved_session_redirect(tmp_path):
    service = BrowserReviewService(
        profile_root=tmp_path,
        franklin_email="researcher@example.org",
        franklin_password="secret",
        navigation_timeout_ms=2_000,
    )

    class Locator:
        def count(self):
            return 0

        def fill(self, value):
            raise AssertionError("Franklin form must not be filled after redirect")

    class Page:
        url = "https://franklin.genoox.com/login"

        def locator(self, selector):
            return Locator()

        def wait_for_timeout(self, milliseconds):
            self.url = "https://franklin.genoox.com/clinical-db/home"

    assert service._try_saved_login("Franklin", Page())


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
    assert evidence.raw["classification"] == "Pathogenic"
    assert evidence.raw["identity_verification"]["basis"] == "exact_transcript"


def test_franklin_queries_use_transcript_then_genomic_fallback():
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC_VPM_1",
        symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup",
        hgvsp="NP_057103.2:p.Arg262ProfsTer26",
        genomic_location="chr7:139097298",
        ref_allele="T",
        alt_allele="TC",
    )

    assert _franklin_queries(variant) == [
        "LUC7L2:c.784dup",
        "chr7-139097298 T>TC",
    ]


def test_franklin_accepts_different_transcript_for_same_grch37_variant():
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC_VPM_1",
        symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup",
        genomic_location="chr7:139097298",
        ref_allele="T",
        alt_allele="TC",
    )
    body = """
    FMC1-LUC7L2:c.982dup
    GRCh37 chr7:139097298 T>TC
    Suggested classification
    Likely pathogenic
    """

    evidence = parse_franklin_page(
        body, variant, "https://franklin.genoox.com/clinical-db/variant/snpTumor/example"
    )

    assert evidence.status == "found"
    assert evidence.raw["identity_verification"]["basis"] == "grch37_genomic"


def test_franklin_rejects_same_gene_at_different_genomic_position():
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC_VPM_1",
        symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup",
        genomic_location="chr7:139097298",
        ref_allele="T",
        alt_allele="TC",
    )
    body = """
    LUC7L2:c.982dup
    GRCh37 chr7:139097299 T>TC
    Suggested classification
    Likely pathogenic
    """

    evidence = parse_franklin_page(
        body, variant, "https://franklin.genoox.com/clinical-db/variant/snpTumor/example"
    )

    assert evidence.status == "identity_mismatch"


def test_franklin_falls_back_after_identity_mismatch(tmp_path, monkeypatch):
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC_VPM_1",
        symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup",
        genomic_location="chr7:139097298",
        ref_allele="T",
        alt_allele="TC",
    )
    service = BrowserReviewService(profile_root=tmp_path)
    calls = []

    def attempt(page, current_variant, query, artifact_directory, *, progress):
        calls.append(query)
        status = "identity_mismatch" if len(calls) == 1 else "found"
        return DatabaseEvidence("Franklin", status, query, accession=query)

    monkeypatch.setattr(service, "_search_franklin_query", attempt, raising=False)

    evidence = service._resolve_franklin_queries(
        object(), variant, tmp_path / "franklin", progress=None
    )

    assert evidence.status == "found"
    assert calls == ["LUC7L2:c.784dup", "chr7-139097298 T>TC"]
    assert evidence.raw["query_attempts"] == calls


def test_franklin_skips_fallback_after_verified_primary_result(tmp_path, monkeypatch):
    variant = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC_VPM_1",
        symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup",
        genomic_location="chr7:139097298",
        ref_allele="T",
        alt_allele="TC",
    )
    service = BrowserReviewService(profile_root=tmp_path)
    calls = []

    def attempt(page, current_variant, query, artifact_directory, *, progress):
        calls.append(query)
        return DatabaseEvidence("Franklin", "found", query, accession=query)

    monkeypatch.setattr(service, "_search_franklin_query", attempt, raising=False)

    evidence = service._resolve_franklin_queries(
        object(), variant, tmp_path / "franklin", progress=None
    )

    assert evidence.status == "found"
    assert calls == ["LUC7L2:c.784dup"]


def test_franklin_category_titles_wait_for_nonempty_de_novo(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)

    class Categories:
        calls = 0

        def all_inner_texts(self):
            self.calls += 1
            if self.calls == 1:
                return ["", ""]
            return ["Case Control Studies\nLoaded", "De Novo Data\nLoaded"]

    class Page:
        waits = []

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()
    titles = service._wait_for_nonempty_category_titles(
        page, Categories(), required_title="De Novo Data"
    )

    assert titles == ["Case Control Studies", "De Novo Data"]
    assert page.waits == [500]


def test_capture_retries_once_only_after_blank_incident(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    path = tmp_path / "capture.png"
    attempts = []

    class Page:
        waits = []

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    def capture():
        attempts.append(1)
        image = Image.new("RGB", (800, 500), "white")
        if len(attempts) == 2:
            image.paste("navy", (20, 20, 780, 300))
        image.save(path)

    page = Page()
    validation = service._capture_with_incident_retry(page, path, capture)

    assert validation.valid
    assert len(attempts) == 2
    assert sum(page.waits) == 5_000


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
    assert evidence.raw["screenshots"][0]["label"] == "Full ACMG classification page"


def test_franklin_assessment_waits_for_dynamic_panels_before_capture(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(
        profile_root=tmp_path, capture_validator=VALID_CAPTURE
    )

    class Heading:
        def __init__(self, name):
            self.name = name

        def wait_for(self, **kwargs):
            pass

        def evaluate(self, script):
            pass

        def bounding_box(self):
            top = 80 if self.name == "Predictions" else 280
            return {"x": 0, "y": top, "width": 900, "height": 40}

    class Section:
        def __init__(self, heading):
            self.heading = heading
            self.captures = []

        def count(self):
            return 1

        def bounding_box(self):
            top = 100 if self.heading.name == "Predictions" else 300
            return {"x": 20, "y": top, "width": 900, "height": 180}

        def evaluate(self, script):
            if "scrollWidth" in script:
                return {"width": 980, "height": 180}
            return {"height": 180, "textLength": 250}

        def screenshot(self, **kwargs):
            self.captures.append(kwargs)

    class Sections:
        def filter(self, *, has):
            return Section(has)

    class Page:
        url = "https://franklin.genoox.com/clinical-db/variant/snp/example"

        def __init__(self):
            self.waits = []
            self.capture = []

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
            self.capture.append(kwargs)

    page = Page()

    screenshots = service._capture_franklin_assessment(page, variant, tmp_path)

    assert page.waits == [500, 500, 500, 500]
    assert [item["label"] for item in screenshots] == [
        "Predictions",
        "Population frequencies",
    ]
    assert screenshots[0]["path"].endswith("-predictions.png")
    assert screenshots[1]["path"].endswith("-population-frequencies.png")
    assert len(page.capture) == 2
    assert all(item["clip"]["width"] == 1016 for item in page.capture)


def test_franklin_computed_capture_uses_both_subtabs_and_skips_somatic(
    tmp_path, monkeypatch
):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)
    selected = []
    requested_tabs = []

    class Locator:
        def __init__(self, label=""):
            self.label = label

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def is_visible(self):
            return False

        def click(self):
            selected.append(self.label)

        def click_physical(self):
            selected.append(self.label)

        def evaluate(self, script):
            selected.append(self.label)
            return True

        def wait_for(self, **kwargs):
            pass

    class Page:
        url = "https://franklin.genoox.com/clinical-db/variant/snpTumor/example"

        def get_by_role(self, role, *, name, exact):
            requested_tabs.append(name)
            return Locator(name)

        def get_by_text(self, text, *, exact):
            return Locator(text)

        def locator(self, selector):
            assert selector in {
                "gnx-mini-app-header img.close-btn",
                "gnx-result-page",
                "gnx-oncogenic-classification-app",
            }
            return Locator()

        def wait_for_timeout(self, milliseconds):
            pass

        def evaluate(self, script):
            pass

    monkeypatch.setattr(
        service,
        "_capture_franklin_classification",
        lambda *args: [{"label": "ACMG", "path": "acmg.png", "url": Page.url}],
    )
    monkeypatch.setattr(
        service,
        "_capture_franklin_oncogenic_classification",
        lambda *args: [
            {"label": "Oncogenic", "path": "oncogenic.png", "url": Page.url}
        ],
    )

    screenshots = service._capture_franklin_computed_pages(
        Page(), variant, tmp_path
    )

    assert requested_tabs == ["Computed Classification"]
    assert "Somatic Clinical Evidence" not in requested_tabs
    assert selected == [
        "Computed Classification",
        "ACMG Classification",
        "Oncogenic Classification",
        "ACMG Classification",
    ]
    assert [item["label"] for item in screenshots] == ["ACMG", "Oncogenic"]


def test_browser_review_can_be_cancelled_before_opening_edge(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = BrowserReviewService(
        profile_root=tmp_path,
        stop_requested=lambda: True,
    )

    with pytest.raises(BrowserReviewCancelled):
        service.search_variants([variant], ["Franklin"], tmp_path / "evidence")


def test_franklin_classification_capture_ends_with_complete_de_novo_card(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(
        profile_root=tmp_path, capture_validator=VALID_CAPTURE
    )

    captures = []

    class Category:
        def __init__(self, index):
            self.index = index

        def screenshot(self, **kwargs):
            captures.append((self.index, kwargs["path"]))

        def scroll_into_view_if_needed(self):
            pass

        def bounding_box(self):
            return {"x": 0, "y": 100 + self.index * 100, "width": 1000, "height": 90}

    class Categories:
        def all_inner_texts(self):
            return [
                "Effect on Protein\nPVS1\nStrong",
                "De Novo Data\nPS2\nStrong",
                "Population Data\nCriteria unmet",
            ]

        def nth(self, index):
            return Category(index)

    class Panel:

        def count(self):
            return 1

        def wait_for(self, **kwargs):
            pass

        def evaluate(self, script, argument=None):
            pass

        def bounding_box(self):
            return {"x": 0, "y": 0, "width": 1000, "height": 500}

        def locator(self, selector):
            assert selector == "gnx-result-category"
            return Categories()

    panel = Panel()
    class Page:
        url = "https://franklin.genoox.com/clinical-db/variant/snp/example"

        def __init__(self):
            self.captures = []

        def locator(self, selector):
            assert selector == "gnx-result-page"
            return panel

        def evaluate(self, script):
            pass

        def screenshot(self, **kwargs):
            self.captures.append(kwargs)

        def wait_for_timeout(self, milliseconds):
            assert milliseconds in {200, 250}

    page = Page()
    screenshots = service._capture_franklin_classification(
        page, variant, tmp_path
    )

    assert len(screenshots) == 3
    assert [item["label"] for item in screenshots] == [
        "ACMG classification overview",
        "ACMG evidence: Effect on Protein",
        "ACMG evidence: De Novo Data",
    ]
    assert [capture[0] for capture in captures] == [0, 1]
    assert page.captures[0]["clip"] == {
        "x": 0,
        "y": 0,
        "width": 1032,
        "height": 100,
    }
    assert not any("Population" in item["label"] for item in screenshots)


def test_franklin_overview_starts_above_visible_gene_header(tmp_path):
    service = BrowserReviewService(
        profile_root=tmp_path, capture_validator=VALID_CAPTURE
    )
    captures = []

    class Element:
        def __init__(self, box):
            self.box = box

        def bounding_box(self):
            return self.box

    class Matches:
        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return Element({"x": 70, "y": 90, "width": 180, "height": 42})

    class Page:
        def evaluate(self, script):
            pass

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 200

        def get_by_text(self, text, *, exact):
            assert text == "TP53"
            assert exact is True
            return Matches()

        def screenshot(self, **kwargs):
            captures.append(kwargs)

    class Panel(Element):
        def evaluate(self, script):
            pass

    page = Page()
    service._capture_franklin_classification_overview(
        page,
        Panel({"x": 100, "y": 150, "width": 900, "height": 600}),
        Element({"x": 100, "y": 360, "width": 900, "height": 100}),
        tmp_path / "overview.png",
        gene_symbol="TP53",
    )

    assert captures[0]["clip"] == {
        "x": 38,
        "y": 66,
        "width": 994,
        "height": 294,
    }


def test_franklin_oncogenic_capture_uses_named_evidence_boxes(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(
        profile_root=tmp_path, capture_validator=VALID_CAPTURE
    )
    captures = []

    class Category:
        def __init__(self, index):
            self.index = index

        def scroll_into_view_if_needed(self):
            pass

        def screenshot(self, **kwargs):
            captures.append((self.index, kwargs["path"]))

        def bounding_box(self):
            return {
                "x": 20,
                "y": 120 + self.index * 120,
                "width": 1000,
                "height": 110,
            }

    class Categories:
        def all_inner_texts(self):
            return [
                "Population Data\nOP4\nSupportingOncogenic",
                "Functional Data\nOVS3\nVeryStrongOncogenic",
                "Predictive Data\nOS1\nStrongOncogenic",
            ]

        def nth(self, index):
            return Category(index)

    class Panel:
        def count(self):
            return 1

        def wait_for(self, **kwargs):
            pass

        def evaluate(self, script, argument=None):
            pass

        def bounding_box(self):
            return {"x": 20, "y": 20, "width": 1000, "height": 600}

        def locator(self, selector):
            assert selector == "gnx-oncogenic-classification-tile"
            return Categories()

    panel = Panel()

    class Page:
        url = "https://franklin.genoox.com/example?app=oncogenic-classification"

        def __init__(self):
            self.captures = []

        def locator(self, selector):
            assert selector == "gnx-oncogenic-classification-app"
            return panel

        def evaluate(self, script):
            pass

        def wait_for_timeout(self, milliseconds):
            assert milliseconds in {200, 250}

        def screenshot(self, **kwargs):
            self.captures.append(kwargs)

    page = Page()
    screenshots = service._capture_franklin_oncogenic_classification(
        page, variant, tmp_path
    )

    assert [item["label"] for item in screenshots] == [
        "Oncogenic classification overview",
        "Oncogenic evidence: Population Data",
        "Oncogenic evidence: Functional Data",
        "Oncogenic evidence: Predictive Data",
    ]
    assert [capture[0] for capture in captures] == [0, 1, 2]
    assert page.captures[0]["clip"]["height"] == 120


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


def test_mtbp_screenshot_rediscovers_target_after_hidden_incident(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)
    attempts = 0

    class Cells:
        def count(self):
            return 0

    class Accordion:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def scroll_into_view_if_needed(self):
            pass

        def screenshot(self, *, path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("hidden")
            image = Image.new("RGB", (800, 500), "white")
            image.paste("navy", (20, 20, 780, 300))
            image.save(path)

    class Row:
        def locator(self, selector):
            return Cells() if selector == "td" else Accordion()

        def is_visible(self):
            return True

        def wait_for(self, **kwargs):
            pass

        def bounding_box(self):
            return None

    class Rows:
        def all_inner_texts(self):
            return ["TP53 Mutation missense p.Arg175His exon 5/11"]

        def nth(self, index):
            return Row()

    class Page:
        waits = []

        def locator(self, selector):
            assert selector == "table tr"
            return Rows()

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    path = service._capture_mtbp_variant_screenshot(Page(), variant, tmp_path)

    assert attempts == 2
    assert path.exists()


def test_mtbp_full_patient_report_is_captured_once(tmp_path):
    calls = []

    class Page:
        def screenshot(self, *, path, full_page):
            calls.append((Path(path), full_page))
            Image.new("RGB", (1200, 800), "white").save(path)

    service = BrowserReviewService(
        profile_root=tmp_path,
        capture_validator=VALID_CAPTURE,
    )

    output = service._capture_mtbp_full_report(
        Page(), tmp_path / "evidence", "ARCHER-20260903T120000Z-test"
    )

    assert calls == [(output, True)]
    assert output.name == "ARCHER-20260903T120000Z-test-full-report.png"


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


def test_mtbp_submits_one_combined_report_for_patient(tmp_path, monkeypatch):
    variants = ArcherTsvReader().read(FIXTURE)[3:5]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
    )
    submitted = []

    def run_one(batch, artifact_directory, *, progress):
        submitted.append([variant.hgvsc for variant in batch])
        return {
            service.variant_key(variant): DatabaseEvidence(
                "MTBP",
                "found",
                "matched",
                url="https://mtbp.org/patients/private/report/1/",
                raw={
                    "screenshots": [
                        {"path": "one.png", "url": "https://mtbp.org/private"}
                    ]
                },
            )
            for variant in batch
        }

    monkeypatch.setattr(service, "_search_mtbp_batch", run_one)
    results = service._search_mtbp(variants, tmp_path / "mtbp", progress=None)

    assert submitted == [[variant.hgvsc for variant in variants]]
    assert len(results) == 2
    assert all(item.url == "" for item in results.values())
    assert all(item.raw["screenshots"][0]["url"] == "" for item in results.values())


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


def test_mtbp_replaces_only_explicitly_rejected_query_with_genomic_fallback():
    accepted, rejected = ArcherTsvReader().read(FIXTURE)[3:5]
    accepted_query = _mtbp_variant_query(accepted)
    rejected_query = _mtbp_variant_query(rejected)
    unmapped = [rejected_query]

    assert _mtbp_retry_query(
        accepted, accepted_query, [accepted_query], unmapped
    ) == accepted_query
    assert _mtbp_retry_query(
        rejected, rejected_query, [rejected_query], unmapped
    ) == _mtbp_genomic_query(rejected)


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


def test_mtbp_rechecks_late_reports_without_resubmitting(tmp_path, monkeypatch):
    variants = ArcherTsvReader().read(FIXTURE)[3:5]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
    )
    calls = []

    def run_one(batch, artifact_directory, *, progress):
        calls.append(("submit", [variant.hgvsc for variant in batch]))
        return {
            service.variant_key(variant): DatabaseEvidence(
                "MTBP",
                "timeout" if index == 0 else "found",
                "timeout" if index == 0 else "found",
                accession=variant.hgvsc,
                raw={"analysis_id": "ARCHER-late"} if index == 0 else {},
            )
            for index, variant in enumerate(batch)
        }

    def recover(pending, artifact_directory, *, progress):
        calls.append(("recover", pending[0][0].hgvsc))
        variant = pending[0][0]
        return {
            service.variant_key(variant): DatabaseEvidence("MTBP", "found", "late")
        }

    monkeypatch.setattr(service, "_search_mtbp_batch", run_one)
    monkeypatch.setattr(service, "_recover_mtbp_timeouts", recover)

    results = service._search_mtbp(variants, tmp_path / "mtbp", progress=None)

    assert calls == [
        ("submit", [variant.hgvsc for variant in variants]),
        ("recover", variants[0].hgvsc),
    ]
    assert results[service.variant_key(variants[0])].status == "found"


def test_mtbp_resume_recovers_retained_reports_before_new_submission(
    tmp_path, monkeypatch
):
    variants = ArcherTsvReader().read(FIXTURE)[2:5]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
    )
    prior_items = [
        DatabaseEvidence(
            "MTBP", "timeout", "pending", raw={"analysis_id": "ARCHER-timeout"}
        ),
        DatabaseEvidence(
            "MTBP",
            "partial_capture",
            "recapture",
            raw={"analysis_id": "ARCHER-partial"},
        ),
        DatabaseEvidence(
            "MTBP",
            "found",
            "captured",
            raw={
                "analysis_id": "ARCHER-cleanup",
                "remote_report_cleanup": {"status": "failed"},
            },
        ),
    ]
    prior_evidence = {
        service.variant_key(variant): [evidence]
        for variant, evidence in zip(variants, prior_items, strict=True)
    }
    recovered_calls = []

    def recover(pending, artifact_directory, *, progress):
        recovered_calls.extend(pending)
        return {
            service.variant_key(variant): DatabaseEvidence(
                "MTBP",
                "found",
                "recovered",
                raw={"remote_report_cleanup": {"status": "deleted"}},
            )
            for variant, _ in pending
        }

    monkeypatch.setattr(service, "_recover_mtbp_timeouts", recover)
    monkeypatch.setattr(
        service,
        "_search_mtbp_batch",
        lambda *args, **kwargs: pytest.fail(
            "retained MTBP reports must be recovered before resubmission"
        ),
    )

    try:
        results = service._search_mtbp(
            variants,
            tmp_path / "mtbp",
            progress=None,
            prior_evidence=prior_evidence,
        )
    except TypeError as exc:
        pytest.fail(f"MTBP resume must accept restored evidence: {exc}")

    assert [evidence for _, evidence in recovered_calls] == prior_items
    assert all(item.status == "found" for item in results.values())


def test_mtbp_resume_does_not_duplicate_report_when_recovery_is_unavailable(
    tmp_path, monkeypatch
):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)
    key = service.variant_key(variant)
    prior = DatabaseEvidence(
        "MTBP", "timeout", "pending", raw={"analysis_id": "ARCHER-pending"}
    )
    def unavailable(*args, **kwargs):
        raise RuntimeError("Edge profile could not start")

    monkeypatch.setattr(service, "_recover_mtbp_timeouts", unavailable)
    monkeypatch.setattr(
        service,
        "_search_mtbp_batch",
        lambda *args, **kwargs: pytest.fail(
            "an unavailable recovery must not create a duplicate report"
        ),
    )

    results = service._search_mtbp(
        [variant],
        tmp_path / "mtbp",
        progress=None,
        prior_evidence={key: [prior]},
    )

    assert results[key] is prior
    assert results[key].raw["analysis_id"] == "ARCHER-pending"


def test_mtbp_resume_resubmits_partial_capture_only_after_confirmed_absence(
    tmp_path, monkeypatch
):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path)
    key = service.variant_key(variant)
    prior = DatabaseEvidence(
        "MTBP",
        "partial_capture",
        "recapture",
        raw={"analysis_id": "ARCHER-gone"},
    )
    submitted = []

    def recover(*args, **kwargs):
        prior.raw["remote_report_recovery"] = {"status": "confirmed_absent"}
        return {key: prior}

    def submit(batch, artifact_directory, *, progress):
        submitted.extend(batch)
        return {key: DatabaseEvidence("MTBP", "found", "new report")}

    monkeypatch.setattr(service, "_recover_mtbp_timeouts", recover)
    monkeypatch.setattr(service, "_search_mtbp_batch", submit)

    results = service._search_mtbp(
        [variant],
        tmp_path / "mtbp",
        progress=None,
        prior_evidence={key: [prior]},
    )

    assert submitted == [variant]
    assert results[key].status == "found"


def test_mtbp_late_recovery_failure_keeps_original_timeout(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
    )
    timeout = DatabaseEvidence(
        "MTBP", "timeout", "late", raw={"analysis_id": "ARCHER-late"}
    )
    monkeypatch.setattr(
        service,
        "_search_mtbp_batch",
        lambda batch, directory, *, progress: {
            service.variant_key(variant): timeout
        },
    )
    monkeypatch.setattr(
        service,
        "_recover_mtbp_timeouts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Edge unavailable")),
    )

    results = service._search_mtbp([variant], tmp_path / "mtbp", progress=None)

    assert results[service.variant_key(variant)] is timeout


def test_mtbp_late_recovery_opens_existing_report_without_form_submission(
    tmp_path, monkeypatch
):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=100)
    analysis_id = "ARCHER-late"

    class Locator:
        def __init__(self, text=""):
            self.text = text

        def inner_text(self, **kwargs):
            return self.text

        def count(self):
            return 0

    class Link:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1

        def click(self):
            self.page.url = "https://mtbp.org/patients/1/sample/1/report/2/"

    class Page:
        def __init__(self):
            self.url = "https://mtbp.org/patients/"

        def get_by_role(self, role, *, name, exact):
            assert (role, name, exact) == ("link", analysis_id, True)
            return Link(self)

        def wait_for_url(self, pattern, *, timeout):
            assert pattern.fullmatch(self.url)

        def locator(self, selector):
            if selector == "body":
                return Locator(
                    "Pipeline version: 7.6.4\nHuman genome: GRCh37/hg19\n"
                    "Cancer type: Blood"
                )
            if selector == "[data-tooltip-html*='VEP:']":
                return Locator()
            raise AssertionError(f"unexpected selector: {selector}")

    page = Page()
    visited = []

    class Context:
        pages = [page]

        def close(self):
            pass

    class Runtime:
        chromium = None

        def __init__(self):
            self.chromium = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def launch_persistent_context(self, *args, **kwargs):
            return Context()

    monkeypatch.setattr(
        service, "_browser_api", lambda: (lambda: Runtime(), Exception, TimeoutError)
    )
    monkeypatch.setattr(
        service,
        "_goto_with_retries",
        lambda page, url, **kwargs: (
            visited.append(url),
            setattr(page, "url", url),
        )[-1],
    )
    monkeypatch.setattr(service, "_session_authenticated", lambda database, page: True)
    monkeypatch.setattr(
        service,
        "_extract_mtbp_rows",
        lambda page: [
            {
                "section": "Putative functionally relevant variants: 1",
                "gene": "TP53",
                "gene_info": "TS SF",
                "alteration": "Mutation\nmissense\np.Arg175His\nexon 5/11",
                "identity_text": "p.Arg175His ENST00000269305.4:c.524G>A",
                "functional_evidence": "Evidence A: Pathogenic",
                "biomarkers": "Not contemplated",
                "source_links": [],
            }
        ],
    )
    screenshot = tmp_path / "mtbp" / "capture.png"
    monkeypatch.setattr(
        service,
        "_capture_mtbp_variant_screenshot",
        lambda page, variant, directory: screenshot,
    )
    deleted = []

    def delete_after_local_persistence(page, candidate_analysis_id):
        audit_path = service._screenshot_path(
            tmp_path / "mtbp", "MTBP", variant
        ).with_suffix(".audit.json")
        assert audit_path.exists()
        deleted.append(candidate_analysis_id)
        return {"status": "deleted", "message": "removed"}

    monkeypatch.setattr(
        service, "_delete_mtbp_report", delete_after_local_persistence
    )
    timeout = DatabaseEvidence(
        "MTBP",
        "timeout",
        "late",
        accession=variant.hgvsc,
        raw={"analysis_id": analysis_id, "query_attempts": [variant.hgvsc]},
    )

    recovered = service._recover_mtbp_timeouts(
        [(variant, timeout)], tmp_path / "mtbp", progress=None
    )

    assert recovered[service.variant_key(variant)].status == "found"
    assert recovered[service.variant_key(variant)].raw["late_report_recovered"] is True
    assert recovered[service.variant_key(variant)].raw["remote_report_cleanup"]["status"] == "deleted"
    assert deleted == [analysis_id]
    assert visited[0] == service.login_url("MTBP")


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


def test_mtbp_delete_is_idempotent_when_exact_report_is_already_absent(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = _FakeMtbpReportsPage(["manual-report"])

    outcome = service._delete_mtbp_report(page, "ARCHER-already-gone")

    assert outcome["status"] == "already_absent"
    assert page.reports == ["manual-report"]


def test_mtbp_resume_retries_failed_cleanup_without_resubmitting(
    tmp_path, monkeypatch
):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    analysis_id = "ARCHER-cleanup-retry"
    page = _FakeMtbpReportsPage(["manual-report", analysis_id])
    service = BrowserReviewService(profile_root=tmp_path)

    class Context:
        pages = [page]

        def close(self):
            pass

    class Runtime:
        chromium = None

        def __init__(self):
            self.chromium = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def launch_persistent_context(self, *args, **kwargs):
            return Context()

    monkeypatch.setattr(
        service, "_browser_api", lambda: (lambda: Runtime(), Exception, TimeoutError)
    )
    monkeypatch.setattr(
        service,
        "_goto_with_retries",
        lambda candidate_page, url, **kwargs: candidate_page.goto(url),
    )
    monkeypatch.setattr(service, "_session_authenticated", lambda *args: True)
    prior = DatabaseEvidence(
        "MTBP",
        "found",
        "captured",
        raw={
            "analysis_id": analysis_id,
            "screenshots": [{"path": "saved.png", "url": ""}],
            "remote_report_cleanup": {"status": "failed"},
        },
    )

    recovered = service._recover_mtbp_timeouts(
        [(variant, prior)], tmp_path / "mtbp", progress=None
    )

    evidence = recovered[service.variant_key(variant)]
    assert evidence is prior
    assert evidence.raw["remote_report_cleanup"]["status"] == "deleted"
    assert page.reports == ["manual-report"]


def test_mtbp_finalization_retains_incomplete_report_for_recovery(
    tmp_path, monkeypatch
):
    service = BrowserReviewService(profile_root=tmp_path)
    evidence = DatabaseEvidence(
        "MTBP",
        "partial_capture",
        "Screenshot requires retry.",
        raw={"analysis_id": "ARCHER-incomplete", "screenshots": []},
    )
    audit_path = tmp_path / "mtbp" / "incomplete.audit.json"
    audit_path.parent.mkdir()

    monkeypatch.setattr(
        service,
        "_delete_mtbp_report",
        lambda *args: pytest.fail("incomplete MTBP reports must not be deleted"),
    )

    outcome = service._finalize_mtbp_report(
        object(), "ARCHER-incomplete", [(audit_path, evidence)]
    )

    assert outcome["status"] == "retained_incomplete"
    assert audit_path.exists()
    assert evidence.raw["remote_report_cleanup"] == outcome


def test_mtbp_preflight_removes_all_old_archer_reports(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    reports = [
        "manual-1",
        "ARCHER-old-1",
        "ARCHER-old-2",
        "manual-2",
        "ARCHER-old-3",
    ]
    page = _FakeMtbpReportsPage(reports)

    outcome = service._cleanup_stale_mtbp_reports(page, progress=None)

    assert page.reports == ["manual-1", "manual-2"]
    assert outcome["status"] == "deleted_stale"
    assert outcome["deleted_stale_reports"] == [
        "ARCHER-old-1",
        "ARCHER-old-2",
        "ARCHER-old-3",
    ]
    assert outcome["remaining_reports"] == 2


def test_mtbp_preflight_removes_archer_reports_even_below_capacity(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    reports = ["manual-1", "ARCHER-pending", "manual-2", "manual-3"]
    page = _FakeMtbpReportsPage(reports)

    outcome = service._cleanup_stale_mtbp_reports(page, progress=None)

    assert page.reports == ["manual-1", "manual-2", "manual-3"]
    assert outcome["status"] == "deleted_stale"
    assert outcome["remaining_reports"] == 3
    assert outcome["remaining_archer_reports"] == 0


def test_mtbp_preflight_refuses_to_delete_five_manual_reports(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)
    page = _FakeMtbpReportsPage([f"manual-{index}" for index in range(5)])

    with pytest.raises(RuntimeError, match="delete an older report manually"):
        service._cleanup_stale_mtbp_reports(page, progress=None)
