from pathlib import Path

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


def test_local_devtools_http_ignores_enterprise_proxy(monkeypatch):
    from archer_processor.services.edge_cdp import _http_json
    import urllib.request

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"Browser":"Edge"}'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'http://127.0.0.1:9222/json/version'
            return Response()

    def build(handler):
        assert isinstance(handler, urllib.request.ProxyHandler)
        assert handler.proxies == {}
        return Opener()

    monkeypatch.setattr(urllib.request, 'build_opener', build)
    assert _http_json('http://127.0.0.1:9222/json/version') == {'Browser': 'Edge'}


def test_devtools_websocket_uses_preconnected_loopback_socket(monkeypatch):
    from archer_processor.services.edge_cdp import _CdpConnection
    from types import SimpleNamespace
    transport = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr('archer_processor.services.edge_cdp.socket.create_connection',
                        lambda address, timeout: transport if address == ('127.0.0.1', 9222) else None)
    def connect(url, **kwargs):
        assert kwargs['socket'] is transport
        assert 'origin' not in kwargs
        # websocket-client otherwise synthesizes an Origin header from the
        # URL host, which Edge 111+ rejects when the port was chosen at
        # runtime. Origin-less local clients stay accepted.
        assert kwargs['suppress_origin'] is True
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr('archer_processor.services.edge_cdp.websocket.create_connection', connect)
    connection = _CdpConnection('ws://127.0.0.1:9222/devtools/page/test')
    connection.close()


def test_failed_launch_closes_only_its_known_browser_endpoint(monkeypatch):
    from archer_processor.services.edge_cdp import _close_failed_browser
    from types import SimpleNamespace
    calls = []
    class Connection:
        def __init__(self, url):
            calls.append(url)
        def call(self, method, **kwargs):
            calls.append(method)
        def close(self):
            calls.append('closed')
    monkeypatch.setattr('archer_processor.services.edge_cdp._CdpConnection', Connection)
    _close_failed_browser(SimpleNamespace(poll=lambda: 0),
                          {'webSocketDebuggerUrl':'ws://127.0.0.1:9222/devtools/browser/ours'})
    assert calls == ['ws://127.0.0.1:9222/devtools/browser/ours', 'Browser.close', 'closed']


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


def test_launch_requests_dynamic_port_and_connects_to_announced_port(tmp_path, monkeypatch):
    from archer_processor.services import edge_cdp

    launches = []
    probes = []

    class Popen:
        def __init__(self, arguments, **kwargs):
            launches.append(list(arguments))
            self.returncode = None
            self._closed = False
            port_file = Path(arguments[[str(a) for a in arguments].index(
                next(a for a in arguments if str(a).startswith("--user-data-dir="))
            )].split("=", 1)[1]) / "DevToolsActivePort"
            port_file.write_text("25687\n/devtools/browser/test\n", encoding="utf-8")

        def poll(self):
            return None if not self._closed else 0

        def wait(self, timeout=None):
            return None

        def terminate(self):
            self._closed = True

    def fake_http_json(url, **kwargs):
        probes.append(url)
        if url.endswith("/json/version"):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:25687/devtools/browser/test"}
        if url.endswith("/json/list"):
            return [{"id": "page-1", "type": "page",
                     "webSocketDebuggerUrl": "ws://127.0.0.1:25687/devtools/page/page-1"}]
        if "/json/new" in url:
            return {"id": "page-1", "type": "page",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:25687/devtools/page/page-1"}
        raise AssertionError(f"unexpected DevTools probe: {url}")

    class Connection:
        def __init__(self, websocket_url):
            probes.append(websocket_url)

        def call(self, method, params=None, **kwargs):
            probes.append(method)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(edge_cdp.subprocess, "Popen", Popen)
    monkeypatch.setattr(edge_cdp, "_http_json", fake_http_json)
    monkeypatch.setattr(edge_cdp, "_CdpConnection", Connection)
    monkeypatch.setattr(edge_cdp, "find_edge_executable", lambda: Path("msedge.exe"))

    context = EdgeCdpContext.launch(
        tmp_path / "profile",
        viewport={"width": 1440, "height": 1000},
        accept_downloads=True,
    )

    arguments = launches[0]
    assert "--remote-debugging-port=0" in arguments
    assert not any("--remote-allow-origins" in str(a) for a in arguments)
    assert "--remote-debugging-address=127.0.0.1" in arguments
    # The context must talk to the port Edge announced in DevToolsActivePort.
    assert context.endpoint == "http://127.0.0.1:25687"
    assert "http://127.0.0.1:25687/json/version" in probes
    assert "http://127.0.0.1:25687/json/list" in probes
    context.close()


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
