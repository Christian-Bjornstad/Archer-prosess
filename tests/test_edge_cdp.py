from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.edge_cdp import (
    EdgeCdpError,
    EdgeCdpContext,
    EdgeCdpLocator,
    EdgeCdpPage,
    EdgeCdpRuntime,
    EdgeCdpTimeout,
    _role_expression,
    _text_expression,
    find_edge_executable,
)


def test_browser_service_uses_edge_cdp_backend(tmp_path):
    service = BrowserReviewService(profile_root=tmp_path)

    factory, error_type, timeout_type = service._browser_api()

    assert factory.__name__ == "sync_edge_cdp"
    assert error_type is EdgeCdpError
    assert timeout_type is EdgeCdpTimeout


def test_find_edge_executable_uses_managed_program_files(tmp_path, monkeypatch):
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"")
    monkeypatch.setattr("archer_processor.services.edge_cdp.shutil.which", lambda _: None)
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert find_edge_executable() == edge.resolve()


def test_role_and_text_locators_encode_accessible_matching():
    role = _role_expression("button", "Sign in", True)
    text = _text_expression("De Novo Data", True)

    assert "aria-label" in role
    assert "placeholder" in role
    assert "actual === expected" in role
    assert "children" in text
    assert "actual === expected" in text


def test_wait_for_url_accepts_predicate():
    class Page(EdgeCdpPage):
        def __init__(self):
            self.urls = iter(
                [
                    "https://example.org/login",
                    "https://example.org/result",
                ]
            )
            self.current = ""

        @property
        def url(self):
            self.current = next(self.urls, self.current)
            return self.current

    Page().wait_for_url(lambda url: url.endswith("/result"), timeout=1_000)


def test_edge_launcher_retries_transient_broker_failures(tmp_path, monkeypatch):
    attempts = []
    sleeps = []
    sentinel = object()

    def launch(profile, *, viewport, accept_downloads, background):
        attempts.append((profile, background))
        if len(attempts) < 3:
            raise EdgeCdpError("stale Edge broker")
        return sentinel

    monkeypatch.setattr(EdgeCdpContext, "launch", launch)
    monkeypatch.setattr("archer_processor.services.edge_cdp.time.sleep", sleeps.append)
    runtime = EdgeCdpRuntime()

    context = runtime.chromium.launch_persistent_context(
        str(tmp_path), background=True
    )

    assert context is sentinel
    assert len(attempts) == 3
    assert all(background is True for _, background in attempts)
    assert sleeps == [1.5, 3.0]


def test_locator_can_scroll_visible_element_before_screenshot():
    class Page:
        def __init__(self):
            self.expression = ""

        def _evaluate_value(self, expression):
            self.expression = expression
            return True

    page = Page()

    EdgeCdpLocator(page, "[document.body]").scroll_into_view_if_needed()

    assert "scrollIntoView" in page.expression
    assert "Element is hidden" in page.expression
