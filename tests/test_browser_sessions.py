from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from archer_processor.services.browser_review import BROWSER_DATABASES, BrowserReviewService


class Page:
    def __init__(self, url, *, selectors=(), controls=(), body=""):
        self.url = url
        self.selectors = set(selectors)
        self.controls = set(controls)
        self.body = body
        self.navigations = []

    def goto(self, url, **kwargs):
        self.navigations.append(url)

    def wait_for_timeout(self, milliseconds):
        pass

    def evaluate(self, script, *, timeout_ms):
        import re
        return {
            "url": self.url,
            "login_form": bool(self.selectors & {"input[type='password']", "#password"}),
            "sign_out": any(re.fullmatch(r"\s*(?:log\s*out|sign\s*out)\s*", text, re.I) for text in self.controls),
            "mtbp_form": "#variant-input" in self.selectors,
            "franklin_search": "input[placeholder='Enter variant, gene or select an example above']" in self.selectors,
            "clinvar_page": "ClinVar" in self.body,
            "access_error": "Access denied" in self.body,
        }


@pytest.mark.parametrize("database,url", [
    ("OncoKB", "https://www.oncokb.org/"),
    ("Franklin", "https://franklin.genoox.com/clinical-db/home"),
    ("COSMIC", "https://cancer.sanger.ac.uk/cosmic/"),
    ("MTBP", "https://mtbp.org/analyse/"),
])
def test_absence_of_login_form_never_confirms_authentication(tmp_path, database, url):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    checker = BrowserSessionCheckService(BrowserReviewService(profile_root=tmp_path))

    result = checker.inspect_page(database, Page(url))

    assert result.status == "unknown"


@pytest.mark.parametrize("database,url,selectors,controls,status", [
    ("COSMIC", "https://cancer.sanger.ac.uk/cosmic/", (), ("Log out",), "authenticated"),
    ("OncoKB", "https://www.oncokb.org/", (), ("Sign out",), "authenticated"),
    ("MTBP", "https://mtbp.org/analyse/", ("#variant-input",), (), "authenticated"),
    ("Franklin", "https://franklin.genoox.com/clinical-db/home", ("input[placeholder='Enter variant, gene or select an example above']",), (), "authenticated"),
    ("Franklin", "https://franklin.genoox.com/login", ("#email", "#password"), (), "login_required"),
    ("OncoKB", "https://www.oncokb.org/login", ("input[type='password']",), (), "login_required"),
    ("MTBP", "https://id.mtbp.org/auth/", ("#username", "#password"), (), "login_required"),
    ("ClinVar", "https://www.ncbi.nlm.nih.gov/clinvar/", (), (), "public"),
])
def test_provider_page_evidence_classifies_session(tmp_path, database, url, selectors, controls, status):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    checker = BrowserSessionCheckService(BrowserReviewService(profile_root=tmp_path))

    result = checker.inspect_page(database, Page(url, selectors=selectors, controls=controls, body="ClinVar"))

    assert result.database == database
    assert result.status == status
    assert result.message


@pytest.mark.parametrize("database,url", [
    ("ClinVar", "about:blank"),
    ("OncoKB", "https://www.oncokb.org.attacker.test/"),
    ("Franklin", "https://franklin.genoox.com/login"),
    ("MTBP", "https://unexpected.test/analyse/"),
])
def test_redirect_or_loading_page_is_not_success(tmp_path, database, url):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    checker = BrowserSessionCheckService(BrowserReviewService(profile_root=tmp_path))

    result = checker.inspect_page(database, Page(url, selectors=("#variant-input",), controls=("Sign out",)))

    assert result.status == "unknown"


def test_check_all_releases_each_profile_and_continues_after_provider_error(tmp_path, monkeypatch):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    review = BrowserReviewService(profile_root=tmp_path, navigation_timeout_ms=2_000)
    pages = {name: Page(review.login_url(name), body="ClinVar" if name == "ClinVar" else "") for name in BROWSER_DATABASES}
    closed = []
    launches = []

    class Context:
        def __init__(self, database):
            self.database = database
            self.pages = [pages[database]]

        def close(self):
            closed.append(self.database)

    def launch(profile, **kwargs):
        name = next(name for name in BROWSER_DATABASES if name.lower() == Path(profile).name)
        launches.append((name, kwargs))
        assert closed == [item[0] for item in launches[:-1] if item[0] != "OncoKB"]
        if name == "OncoKB":
            raise RuntimeError("managed Edge blocked startup")
        return Context(name)

    @contextmanager
    def browser():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    monkeypatch.setattr(review, "_browser_api", lambda: (browser, RuntimeError, TimeoutError))
    emitted = []

    results = BrowserSessionCheckService(review, settle_timeout_ms=0).check_all(on_result=emitted.append)

    assert [result.database for result in results] == list(BROWSER_DATABASES)
    assert [result.status for result in results] == ["unknown", "error", "unknown", "public", "unknown"]
    assert emitted == results
    assert closed == ["COSMIC", "Franklin", "ClinVar", "MTBP"]
    assert all(kwargs["accept_downloads"] is False for _, kwargs in launches)
    assert all(page.navigations == [review.login_url(name)] for name, page in pages.items() if name != "OncoKB")


def test_check_all_stops_before_starting_a_profile_when_cancelled(tmp_path, monkeypatch):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    review = BrowserReviewService(profile_root=tmp_path, stop_requested=lambda: True)
    monkeypatch.setattr(review, "_browser_api", lambda: pytest.fail("No browser should start"))

    results = BrowserSessionCheckService(review).check_all()

    assert len(results) == len(BROWSER_DATABASES)
    assert all(result.status == "unknown" and "cancel" in result.message.lower() for result in results)


def test_clinvar_requires_provider_content_for_public_access(tmp_path):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    checker = BrowserSessionCheckService(BrowserReviewService(profile_root=tmp_path))

    result = checker.inspect_page("ClinVar", Page("https://www.ncbi.nlm.nih.gov/clinvar/", body="Loading"))

    assert result.status == "unknown"


def test_session_dom_probe_is_one_bounded_call(tmp_path):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    class BoundedPage:
        def evaluate(self, script, *, timeout_ms):
            assert timeout_ms <= 2_000
            return {"url": "https://www.oncokb.org/", "login_form": False, "sign_out": True,
                    "mtbp_form": False, "franklin_search": False, "clinvar_page": False, "access_error": False}

    result = BrowserSessionCheckService(BrowserReviewService(profile_root=tmp_path)).inspect_page("OncoKB", BoundedPage())

    assert result.status == "authenticated"


def test_malformed_session_probe_cannot_confirm_access(tmp_path):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    page = SimpleNamespace(evaluate=lambda *args, **kwargs: {"url": "https://www.oncokb.org/", "sign_out": "false"})

    result = BrowserSessionCheckService(BrowserReviewService(profile_root=tmp_path)).inspect_page("OncoKB", page)

    assert result.status == "unknown"


def test_unrelated_redirect_with_password_form_does_not_claim_provider_login(tmp_path):
    from archer_processor.services.browser_sessions import BrowserSessionCheckService

    page = Page("https://unrelated.example/login", selectors=("#password",))

    result = BrowserSessionCheckService(BrowserReviewService(profile_root=tmp_path)).inspect_page("OncoKB", page)

    assert result.status == "unknown"
