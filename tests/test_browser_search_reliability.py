from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewService,
    _clinvar_identity,
    _matching_clinvar_links,
    parse_franklin_page,
    parse_oncokb_page,
)
from archer_processor.services.provider_failures import (
    ProviderFailureKind,
    ProviderLookupError,
)

CLINVAR_URL = "https://www.ncbi.nlm.nih.gov/clinvar/variation/12374/"
CLINVAR_BODY = (
    "NM_000546.6(TP53):c.524G>A (p.Arg175His)\n"
    "Variation ID: 12374 Accession: VCV000012374.86\n"
    "Location\n17: 7578406 (GRCh37)"
)


def requested_variant():
    return VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC_VPM_1",
        symbol="TP53",
        hgvsc="NM_000546.6:c.524G>A",
        hgvsp="NP_000537.3:p.Arg175His",
        genomic_location="chr17:7578406",
        ref_allele="G",
        alt_allele="A",
        cosmic_id="COSM10648",
    )


class ClinVarPage:
    def __init__(self, *, delayed_search=False, delayed_record=False, stalled=False):
        self.url = ""
        self.visited = []
        self.waits = []
        self.delayed_search = delayed_search
        self.delayed_record = delayed_record
        self.stalled = stalled
        self.rendered = False

    def goto(self, url, **kwargs):
        self.url = url
        self.visited.append(url)
        self.rendered = False

    def locator(self, selector):
        page = self

        class Locator:
            def inner_text(self, **kwargs):
                if page.stalled:
                    return "ClinVar\nLoading..."
                if "/variation/" in page.url:
                    if page.delayed_record and not page.rendered:
                        return "Variation ID: 12374\nLoading..."
                    return CLINVAR_BODY
                if page.delayed_search and not page.rendered:
                    return "ClinVar\nSearch results\nLoading..."
                return "Search results\nNM_000546.6(TP53):c.524G>A"

            def evaluate_all(self, script):
                if page.stalled or (page.delayed_search and not page.rendered):
                    return []
                return [{"text": "NM_000546.6(TP53):c.524G>A", "href": CLINVAR_URL}]

        assert selector in {"body", "a[href*='/clinvar/variation/']"}
        return Locator()

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.rendered = True


@pytest.mark.parametrize(
    "delayed_search,delayed_record", [(True, False), (False, True)]
)
def test_clinvar_waits_for_rendered_identity_before_falling_back(
    tmp_path, monkeypatch, delayed_search, delayed_record
):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)
    page = ClinVarPage(delayed_search=delayed_search, delayed_record=delayed_record)
    monkeypatch.setattr(service, "_capture_clinvar_result", lambda v, e, p, d: e)

    evidence = service._lookup_clinvar_variant(page, requested_variant(), tmp_path)

    assert evidence.status == "found"
    assert evidence.raw["identity_verification"]["accepted"]
    assert evidence.raw["query_attempts"] == ["TP53 R175H"]
    assert page.visited == [
        "https://www.ncbi.nlm.nih.gov/clinvar/?term=TP53%20R175H",
        CLINVAR_URL,
    ]
    assert page.waits


@pytest.mark.parametrize(
    "partial_identity",
    [
        "TP53",
        "NM_000546.6:c.524G>A (p.Arg175His)",
        "HGVS\nc.524G>A\nLoading variant details...",
        "Name\nc.524G>A",
        "HGVS c.524G>A",
        "NM_000546:c.524G>A",
    ],
)
def test_clinvar_waits_when_record_metadata_precedes_gene_and_hgvs(
    tmp_path, monkeypatch, partial_identity
):
    class MetadataFirstPage(ClinVarPage):
        def locator(self, selector):
            original = super().locator(selector)
            page = self

            class Locator:
                def inner_text(self, **kwargs):
                    if "/variation/" in page.url and not page.rendered:
                        return (
                            f"{partial_identity}\nVariation ID: 12374\n"
                            "Location\n17: 7578406 (GRCh37)"
                        )
                    return original.inner_text(**kwargs)

                def evaluate_all(self, script):
                    return original.evaluate_all(script)

            return Locator()

    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)
    page = MetadataFirstPage()
    monkeypatch.setattr(service, "_capture_clinvar_result", lambda v, e, p, d: e)

    evidence = service._lookup_clinvar_variant(page, requested_variant(), tmp_path)

    assert evidence.status == "found"
    assert evidence.raw["identity_verification"]["accepted"]
    assert evidence.raw["query_attempts"] == ["TP53 R175H"]
    assert page.visited.count(CLINVAR_URL) == 1
    assert page.waits


def test_clinvar_complete_different_identity_is_processed_without_render_wait(tmp_path):
    class DifferentIdentityPage(ClinVarPage):
        def locator(self, selector):
            original = super().locator(selector)
            page = self

            class Locator:
                def inner_text(self, **kwargs):
                    text = original.inner_text(**kwargs)
                    if "/variation/" in page.url:
                        return text.replace(
                            "NM_000546.6(TP53):c.524G>A (p.Arg175His)",
                            "NM_004333.6(BRAF):c.1799T>A (p.Val600Glu)",
                        )
                    return text

                def evaluate_all(self, script):
                    return original.evaluate_all(script)

            return Locator()

    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)
    page = DifferentIdentityPage()

    evidence = service._lookup_clinvar_variant(page, requested_variant(), tmp_path)

    assert evidence.status == "not_found"
    assert len(evidence.raw["query_attempts"]) == 3
    assert page.visited.count(CLINVAR_URL) == 1
    assert page.waits == []


def test_clinvar_unfinished_page_remains_retryable_without_blind_fallbacks(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)
    page = ClinVarPage(stalled=True)

    evidence = service._lookup_clinvar_variant(page, requested_variant(), tmp_path)

    assert evidence.status == "error"
    assert evidence.raw["failure_kind"] == "transient"
    assert evidence.raw["failure_stage"] == "result_rendering"
    assert evidence.raw["query_attempts"] == ["TP53 R175H"]
    assert len(page.visited) == 1


def test_clinvar_does_not_reopen_a_record_rejected_by_identity(tmp_path):
    class WrongVariantPage(ClinVarPage):
        def locator(self, selector):
            original = super().locator(selector)
            page = self

            class Locator:
                def inner_text(self, **kwargs):
                    text = original.inner_text(**kwargs)
                    return (
                        text.replace("7578406", "7578407")
                        if "/variation/" in page.url
                        else text
                    )

                def evaluate_all(self, script):
                    return original.evaluate_all(script)

            return Locator()

    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)
    page = WrongVariantPage()

    evidence = service._lookup_clinvar_variant(page, requested_variant(), tmp_path)

    assert evidence.status == "not_found"
    assert page.visited.count(CLINVAR_URL) == 1
    assert len(evidence.raw["query_attempts"]) == 3


def test_clinvar_explicit_empty_results_allow_ordered_fallbacks(tmp_path):
    class EmptyPage(ClinVarPage):
        def locator(self, selector):
            class EmptyLocator:
                def inner_text(self, **kwargs):
                    return "Search results\nNo items found."

                def evaluate_all(self, script):
                    return []

            return EmptyLocator()

    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)
    page = EmptyPage()

    evidence = service._lookup_clinvar_variant(page, requested_variant(), tmp_path)

    assert evidence.status == "not_found"
    assert evidence.raw["query_attempts"] == [
        "TP53 R175H",
        "NM_000546.6:c.524G>A",
        "TP53[gene] AND 17[chr] AND 7578406[chrpos37]",
    ]
    assert page.waits == []


class CosmicPage:
    def __init__(self, *, search_links=(), text="Loading...", navigation_error=None):
        self.url = "https://cancer.sanger.ac.uk/cosmic/search?q=COSM10648"
        self.search_links = list(search_links)
        self.text = text
        self.navigation_error = navigation_error
        self.visited = []
        self.waits = []

    def goto(self, url, **kwargs):
        self.visited.append(url)
        if self.navigation_error:
            raise self.navigation_error
        self.url = url

    def locator(self, selector):
        page = self

        class Locator:
            def evaluate_all(self, script):
                if "mutation/overview" in selector:
                    return page.search_links
                return []

            def inner_text(self, **kwargs):
                return page.text

        return Locator()

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


def test_cosmic_missing_rendered_link_is_retryable_instead_of_not_found(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    page = CosmicPage()

    with pytest.raises(ProviderLookupError) as failure:
        service._resolve_cosmic_mutation_page(page, requested_variant())

    assert failure.value.kind == ProviderFailureKind.TRANSIENT


def test_cosmic_explicit_not_found_stops_before_full_render_timeout(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=45_000)
    page = CosmicPage(
        text="Mutation not found\nThe mutation with ID 10648 was not found in our database."
    )

    with pytest.raises(ProviderLookupError) as failure:
        service._resolve_cosmic_mutation_page(page, requested_variant())

    assert failure.value.kind == ProviderFailureKind.NOT_FOUND
    assert page.waits == []


def test_cosmic_expired_login_is_recognized_before_mutation_resolution(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=45_000)
    page = CosmicPage()
    page.url = "https://cancer.sanger.ac.uk/cosmic/login"

    with pytest.raises(ProviderLookupError) as failure:
        service._resolve_cosmic_mutation_page(page, requested_variant())

    assert failure.value.kind == ProviderFailureKind.LOGIN_REQUIRED
    assert page.waits == []


def test_cosmic_does_not_resolve_an_identifier_with_matching_numeric_prefix(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    unrelated_url = (
        "https://cancer.sanger.ac.uk/cosmic/mutation/overview?id=99&merge=106480"
    )
    page = CosmicPage(search_links=[unrelated_url])

    with pytest.raises(ProviderLookupError) as failure:
        service._resolve_cosmic_mutation_page(page, requested_variant())

    assert failure.value.kind == ProviderFailureKind.TRANSIENT
    assert unrelated_url not in page.visited


def test_cosmic_retries_classified_transient_failure_once(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    page = CosmicPage()
    resolutions = 0

    def resolve(page, variant):
        nonlocal resolutions
        resolutions += 1
        if resolutions == 1:
            raise ProviderLookupError(
                ProviderFailureKind.TRANSIENT, "Links are still rendering."
            )

    monkeypatch.setattr(service, "_resolve_cosmic_mutation_page", resolve)
    monkeypatch.setattr(service, "_wait_for_cosmic_result", lambda page: None)
    monkeypatch.setattr(
        service,
        "_capture_cosmic_result",
        lambda v, p, d: DatabaseEvidence("COSMIC", "found", "verified"),
    )

    evidence = service._lookup_cosmic_with_retry(
        page,
        requested_variant(),
        "https://cancer.sanger.ac.uk/cosmic/search?q=COSM10648",
        tmp_path,
        progress=None,
    )

    assert evidence.status == "found"
    assert len(page.visited) == 2


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("Invalid selector"),
        RuntimeError("Edge navigation failed: net::ERR_CERT_DATE_INVALID"),
    ],
)
def test_cosmic_does_not_repeat_deterministic_browser_failures(tmp_path, failure):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    page = CosmicPage(navigation_error=failure)

    with pytest.raises(type(failure), match=str(failure)):
        service._lookup_cosmic_with_retry(
            page,
            requested_variant(),
            "https://cancer.sanger.ac.uk/cosmic/search?q=COSM10648",
            tmp_path,
            progress=None,
        )

    assert len(page.visited) == 1


def test_cosmic_retries_known_network_interruption_without_changing_identity(
    tmp_path, monkeypatch
):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)
    query_url = "https://cancer.sanger.ac.uk/cosmic/search?q=COSM10648"

    class InterruptedPage(CosmicPage):
        def goto(self, url, **kwargs):
            if not self.visited:
                self.visited.append(url)
                raise RuntimeError("Edge navigation failed: net::ERR_CONNECTION_RESET")
            super().goto(url, **kwargs)

    page = InterruptedPage()
    monkeypatch.setattr(
        service, "_resolve_cosmic_mutation_page", lambda page, variant: None
    )
    monkeypatch.setattr(service, "_wait_for_cosmic_result", lambda page: None)
    monkeypatch.setattr(
        service,
        "_capture_cosmic_result",
        lambda v, p, d: DatabaseEvidence("COSMIC", "found", "verified"),
    )

    evidence = service._lookup_cosmic_with_retry(
        page, requested_variant(), query_url, tmp_path, progress=None
    )

    assert evidence.status == "found"
    assert page.visited == [query_url, query_url]


@pytest.mark.parametrize("returned_genomic", ["chr17:7578407 G>A", "chr17:7578406 G>T"])
def test_franklin_explicit_genomic_conflict_overrides_matching_cdna(returned_genomic):
    evidence = parse_franklin_page(
        f"TP53:c.524G>A\nGRCh37 {returned_genomic}\nSuggested classification\nPathogenic",
        requested_variant(),
        "https://franklin.genoox.com/clinical-db/variant/snpTumor/example",
    )

    assert evidence.status == "identity_mismatch"
    assert evidence.clinical_significance == ""
    assert not evidence.raw["identity_verification"]["accepted"]


@pytest.mark.parametrize("identity", ["TP532:c.524G>A", "TP53:c.524G>AT"])
def test_franklin_does_not_accept_identity_prefixes(identity):
    evidence = parse_franklin_page(
        f"{identity}\nSuggested classification\nPathogenic",
        requested_variant(),
        "https://franklin.genoox.com/example",
    )

    assert evidence.status == "identity_mismatch"


def test_clinvar_does_not_accept_longer_cdna_change_with_matching_prefix():
    body = CLINVAR_BODY.replace("c.524G>A", "c.524G>AT")

    assert not _clinvar_identity(body, requested_variant()).accepted
    assert (
        _matching_clinvar_links(
            [{"text": body, "href": CLINVAR_URL}], requested_variant()
        )
        == []
    )


@pytest.mark.parametrize("identity", ["TP53 R175Hfs", "TP532 R175H"])
def test_oncokb_does_not_accept_identity_prefixes(identity):
    evidence = parse_oncokb_page(
        f"{identity}\nVariant Overview\nMutation Effect\nOncogenicity\nOncogenic",
        requested_variant(),
        "https://www.oncokb.org/gene/TP53/somatic/R175H",
    )

    assert evidence.status == "identity_mismatch"


def test_oncokb_does_not_accept_genomic_url_with_matching_allele_prefix():
    evidence = parse_oncokb_page(
        "TP53\nVariant Overview\nMutation Effect\nOncogenicity\nOncogenic",
        requested_variant(),
        "https://www.oncokb.org/hgvsg/17:g.7578406G%3EAT?refGenome=GRCh37",
    )

    assert evidence.status == "identity_mismatch"


@pytest.mark.parametrize(
    "kind", [ProviderFailureKind.LAYOUT_CHANGED, ProviderFailureKind.AMBIGUOUS]
)
def test_franklin_does_not_switch_queries_after_deterministic_failure(
    tmp_path, monkeypatch, kind
):
    service = BrowserReviewService(profile_root=tmp_path)
    queries = []

    def search(page, variant, query, directory, *, progress):
        queries.append(query)
        return DatabaseEvidence(
            "Franklin",
            "error",
            "Provider requires intervention.",
            raw={"failure_kind": kind.value},
        )

    monkeypatch.setattr(service, "_search_franklin_query", search)

    evidence = service._resolve_franklin_queries(
        object(), requested_variant(), tmp_path, progress=None
    )

    assert evidence.raw["failure_kind"] == kind.value
    assert evidence.raw["query_attempts"] == ["TP53:c.524G>A"]
    assert queries == ["TP53:c.524G>A"]


def test_franklin_does_not_repeat_classified_layout_failure_on_later_pass(
    tmp_path, monkeypatch
):
    service = BrowserReviewService(profile_root=tmp_path, franklin_attempts=3)
    lookups = []

    class Context:
        pages = [object()]

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

    def resolve(page, variant, directory, *, progress):
        lookups.append(variant)
        return DatabaseEvidence(
            "Franklin",
            "error",
            "Selectors changed.",
            raw={"failure_kind": "layout_changed"},
        )

    monkeypatch.setattr(
        service, "_browser_api", lambda: (lambda: Runtime(), Exception, TimeoutError)
    )
    monkeypatch.setattr(service, "_resolve_franklin_queries", resolve)
    monkeypatch.setattr(service, "_wait_between_queries", lambda *args, **kwargs: None)

    results = service._search_franklin([requested_variant()], tmp_path, progress=None)

    assert next(iter(results.values())).raw["failure_kind"] == "layout_changed"
    assert len(lookups) == 1


def test_franklin_selector_failure_is_classified_before_retry_policy(
    tmp_path, monkeypatch
):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=500)

    class Search:
        def wait_for(self, **kwargs):
            pass

    class Page:
        url = "https://franklin.genoox.com/clinical-db/home"

        def goto(self, url, **kwargs):
            pass

        def locator(self, selector):
            return Search()

        def get_by_role(self, role):
            class EmptySelectors:
                def count(self):
                    return 0

            return EmptySelectors()

        def wait_for_timeout(self, milliseconds):
            pass

    monkeypatch.setattr(
        service, "_browser_api", lambda: (None, Exception, TimeoutError)
    )

    evidence = service._search_franklin_query(
        Page(), requested_variant(), "TP53:c.524G>A", tmp_path, progress=None
    )

    assert evidence.status == "layout_changed"
    assert evidence.raw["failure_kind"] == "layout_changed"
    assert evidence.raw["failure_stage"] == "selecting the hg19 somatic search mode"


def test_franklin_waits_for_delayed_reference_and_type_selectors(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=1_000)

    class Page:
        waits = []

        def get_by_role(self, role):
            page = self

            class Selectors:
                def count(self):
                    return 2 if page.waits else 0

                def nth(self, index):
                    class Selector:
                        def inner_text(self):
                            return "hg19" if index == 0 else "Somatic"

                    return Selector()

            return Selectors()

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()

    service._select_franklin_search_mode(page)

    assert page.waits
