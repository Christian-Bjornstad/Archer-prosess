from pathlib import Path
from types import SimpleNamespace

import pytest

from archer_processor.services import edge_cdp


@pytest.mark.parametrize("environment_name", ["PROGRAMFILES", "LOCALAPPDATA"])
def test_installed_edge_is_found_in_system_and_per_user_locations(tmp_path, monkeypatch, environment_name):
    executable = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setattr(edge_cdp.shutil, "which", lambda _: None)
    for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(environment_name, str(tmp_path))

    assert edge_cdp.find_edge_executable() == executable.resolve()


@pytest.mark.parametrize("policy,value", [("RemoteDebuggingAllowed", 0), ("UserDataDir", "C:\\Managed\\Edge")])
def test_managed_policy_blocks_launch_before_any_profile_is_changed(tmp_path, monkeypatch, policy, value):
    from archer_processor.services import edge_diagnostics

    monkeypatch.setattr(edge_diagnostics, "_read_edge_policy_values", lambda: [(policy, value, "HKLM")])
    monkeypatch.setattr(edge_cdp.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Edge must not launch"))
    profile = tmp_path / "evidence-profile"

    with pytest.raises(edge_cdp.EdgeCdpError, match=policy):
        edge_cdp.EdgeCdpContext.launch(profile, viewport={"width": 1000, "height": 800}, accept_downloads=False)

    assert not profile.exists()


def test_policy_read_error_does_not_silently_allow_isolation(tmp_path, monkeypatch):
    from archer_processor.services import edge_diagnostics

    def denied():
        raise PermissionError("registry denied")

    monkeypatch.setattr(edge_diagnostics, "_read_edge_policy_values", denied)
    monkeypatch.setattr(edge_cdp.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Edge must not launch"))

    with pytest.raises(edge_cdp.EdgeCdpError, match="policy.*read|read.*polic"):
        edge_cdp.EdgeCdpContext.launch(tmp_path / "profile", viewport={"width": 1000, "height": 800}, accept_downloads=False)


def test_policy_failure_is_not_retried(monkeypatch, tmp_path):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append(1)
        raise edge_cdp.EdgeCdpPolicyError("RemoteDebuggingAllowed blocks debugging")

    monkeypatch.setattr(edge_cdp.EdgeCdpContext, "launch", blocked)
    monkeypatch.setattr(edge_cdp.time, "sleep", lambda _: pytest.fail("Policy failure should not be retried"))

    with pytest.raises(edge_cdp.EdgeCdpError, match="RemoteDebuggingAllowed"):
        edge_cdp.EdgeCdpRuntime().chromium.launch_persistent_context(str(tmp_path))

    assert attempts == [1]


def test_active_evidence_profile_is_not_launched_or_closed(tmp_path, monkeypatch):
    from archer_processor.services import edge_diagnostics

    monkeypatch.setattr(edge_diagnostics, "_read_edge_policy_values", lambda: [])
    profile = tmp_path / "profile"
    profile.mkdir()
    announcement = profile / "DevToolsActivePort"
    announcement.write_text("25687\n/devtools/browser/existing\n")
    monkeypatch.setattr(edge_cdp, "find_edge_executable", lambda: Path("msedge.exe"))
    monkeypatch.setattr(edge_cdp, "_http_json", lambda *args, **kwargs: {"Browser": "Edge"})
    monkeypatch.setattr(edge_cdp.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Existing Edge must not be launched"))
    monkeypatch.setattr(edge_cdp, "_close_failed_browser", lambda *args: pytest.fail("Existing Edge must not be closed"))

    with pytest.raises(edge_cdp.EdgeCdpError, match="already in use"):
        edge_cdp.EdgeCdpContext.launch(profile, viewport={"width": 1000, "height": 800}, accept_downloads=False)

    assert announcement.read_text() == "25687\n/devtools/browser/existing\n"


def test_diagnostics_reports_only_proxy_names_and_does_not_start_browser(monkeypatch):
    from archer_processor.services import edge_diagnostics

    monkeypatch.setattr(edge_diagnostics, "_read_edge_policy_values", lambda: [("RemoteDebuggingAllowed", 1, "HKLM")])
    monkeypatch.setenv("HTTPS_PROXY", "http://secret-user:secret-password@proxy.example:8080")
    monkeypatch.setattr(edge_cdp.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Read-only diagnostics must not launch"))

    result = edge_diagnostics.collect_edge_diagnostics()

    assert "HTTPS_PROXY" in result.proxy_environment_names
    assert "secret-password" not in repr(result)
    assert result.launch_blocked is False


def test_local_devtools_http_rejects_remote_address_before_proxy_bypass(monkeypatch):
    monkeypatch.setattr(edge_cdp.urllib.request, "build_opener", lambda *args: pytest.fail("Remote request must not start"))

    with pytest.raises(edge_cdp.EdgeCdpError, match="loopback"):
        edge_cdp._http_json("http://example.test:9222/json/version")


def test_registry_reader_checks_machine_and_user_32_and_64_bit_views(monkeypatch):
    import sys
    from archer_processor.services import edge_diagnostics

    opened = []
    monkeypatch.setattr(edge_diagnostics, "os", SimpleNamespace(name="nt"))

    class Key:
        def __init__(self, hive):
            self.hive = hive
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def open_key(hive, path, reserved, access):
        opened.append((hive, access))
        assert path == r"SOFTWARE\Policies\Microsoft\Edge"
        return Key(hive)

    def query(key, name):
        if name == "UserDataDir":
            raise FileNotFoundError()
        return (0 if key.hive == "machine" else 1), 4

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_LOCAL_MACHINE="machine", HKEY_CURRENT_USER="user", KEY_READ=1,
        KEY_WOW64_64KEY=256, KEY_WOW64_32KEY=512, REG_DWORD=4, REG_SZ=1,
        OpenKey=open_key, QueryValueEx=query,
    ))

    values = edge_diagnostics._read_edge_policy_values()

    assert opened == [("machine", 257), ("machine", 513), ("user", 257), ("user", 513)]
    assert values == [("RemoteDebuggingAllowed", 0, "HKLM"), ("RemoteDebuggingAllowed", 1, "HKCU")]


def test_page_evaluate_forwards_explicit_timeout():
    class Page(edge_cdp.EdgeCdpPage):
        def __init__(self):
            pass

        def _evaluate_value(self, expression, *, timeout_ms):
            assert timeout_ms == 2_000
            return {"ready": True}

    assert Page().evaluate("() => ({ready: true})", timeout_ms=2_000) == {"ready": True}


def test_local_devtools_http_never_follows_redirects():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    requested = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            if "final" not in self.path:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/json/version?final=1")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"Browser":"redirect target"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        with pytest.raises(edge_cdp.EdgeCdpError, match="redirect"):
            edge_cdp._http_json(f"http://127.0.0.1:{server.server_port}/json/version")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert requested == ["/json/version"]


@pytest.mark.parametrize("failure", ["timeout", "unreadable", "malformed", "ambiguous"])
def test_uncertain_existing_profile_is_not_unlinked_or_launched(tmp_path, monkeypatch, failure):
    from archer_processor.services import edge_diagnostics

    monkeypatch.setattr(edge_diagnostics, "_read_edge_policy_values", lambda: [])
    monkeypatch.setattr(edge_cdp, "find_edge_executable", lambda: Path("msedge.exe"))
    profile = tmp_path / "profile"
    profile.mkdir()
    announcement = profile / "DevToolsActivePort"
    original = "not-a-port\n" if failure == "malformed" else "25687\n/devtools/browser/existing\n"
    announcement.write_text(original)
    if failure == "unreadable":
        monkeypatch.setattr(edge_cdp, "_read_devtools_active_port", lambda _: (_ for _ in ()).throw(PermissionError("denied")))
    else:
        def uncertain(*args, **kwargs):
            if failure == "timeout":
                raise edge_cdp.EdgeCdpTimeout("endpoint timed out")
            raise edge_cdp.EdgeCdpError("invalid endpoint response")
        monkeypatch.setattr(edge_cdp, "_http_json", uncertain)
    monkeypatch.setattr(edge_cdp.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Uncertain profile must not launch"))
    monkeypatch.setattr(edge_cdp, "_close_failed_browser", lambda *args: pytest.fail("Existing Edge must not close"))

    with pytest.raises(edge_cdp.EdgeCdpProfileInUse):
        edge_cdp.EdgeCdpContext.launch(profile, viewport={"width": 1000, "height": 800}, accept_downloads=False)

    assert announcement.read_text() == original


def test_only_explicit_connection_refusal_confirms_stale_profile(tmp_path, monkeypatch):
    import urllib.error

    announcement = tmp_path / "DevToolsActivePort"
    announcement.write_text("25687\n/devtools/browser/stale\n")

    def refused(*args, **kwargs):
        try:
            raise urllib.error.URLError(ConnectionRefusedError("listener is closed"))
        except urllib.error.URLError as exc:
            raise edge_cdp.EdgeCdpError("DevTools endpoint unavailable") from exc

    monkeypatch.setattr(edge_cdp, "_http_json", refused)

    edge_cdp._ensure_profile_idle(tmp_path)

    # Occupancy checking does not itself delete the announcement.
    assert announcement.exists()


def test_os_profile_lease_excludes_another_process_and_releases_on_close(tmp_path):
    import subprocess
    import sys

    profile = tmp_path / "profile"
    profile.mkdir()
    script = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from archer_processor.services.edge_cdp import _acquire_profile_lease, EdgeCdpProfileInUse
try:
    lease = _acquire_profile_lease(Path(sys.argv[2]))
except EdgeCdpProfileInUse:
    print('blocked')
else:
    lease.close()
    print('acquired')
"""
    arguments = [sys.executable, "-c", script, str(Path(edge_cdp.__file__).parents[2]), str(profile)]
    lease = edge_cdp._acquire_profile_lease(profile)
    try:
        blocked = subprocess.run(arguments, capture_output=True, text=True, timeout=10, check=True)
    finally:
        lease.close()
    acquired = subprocess.run(arguments, capture_output=True, text=True, timeout=10, check=True)

    assert blocked.stdout.strip() == "blocked"
    assert acquired.stdout.strip() == "acquired"


def test_context_holds_lease_during_launch_and_shutdown(tmp_path, monkeypatch):
    from archer_processor.services import edge_diagnostics

    monkeypatch.setattr(edge_diagnostics, "_read_edge_policy_values", lambda: [])
    monkeypatch.setattr(edge_cdp, "find_edge_executable", lambda: Path("msedge.exe"))
    profile = tmp_path / "profile"
    lifecycle = []

    def require_exclusion(stage):
        with pytest.raises(edge_cdp.EdgeCdpProfileInUse):
            edge_cdp._acquire_profile_lease(profile)
        lifecycle.append(stage)

    def launch(cls, directory, edge, **kwargs):
        require_exclusion("launch")
        return cls(SimpleNamespace(), "http://127.0.0.1:25687", directory)

    monkeypatch.setattr(edge_cdp.EdgeCdpContext, "_launch_owned", classmethod(launch))
    context = edge_cdp.EdgeCdpContext.launch(profile, viewport={"width": 1000, "height": 800}, accept_downloads=False)
    monkeypatch.setattr(context, "_close_owned_browser", lambda: require_exclusion("shutdown"))

    require_exclusion("running")
    context.close()
    lease = edge_cdp._acquire_profile_lease(profile)
    lease.close()

    assert lifecycle == ["launch", "running", "shutdown"]


@pytest.mark.parametrize("stage", ["launch", "close"])
def test_profile_lease_released_after_lifecycle_error(tmp_path, monkeypatch, stage):
    from archer_processor.services import edge_diagnostics

    monkeypatch.setattr(edge_diagnostics, "_read_edge_policy_values", lambda: [])
    monkeypatch.setattr(edge_cdp, "find_edge_executable", lambda: Path("msedge.exe"))
    profile = tmp_path / "profile"

    def launch(cls, directory, edge, **kwargs):
        if stage == "launch":
            raise edge_cdp.EdgeCdpError("test lifecycle failure")
        return cls(SimpleNamespace(), "http://127.0.0.1:25687", directory)

    monkeypatch.setattr(edge_cdp.EdgeCdpContext, "_launch_owned", classmethod(launch))
    if stage == "launch":
        with pytest.raises(edge_cdp.EdgeCdpError, match="test lifecycle failure"):
            edge_cdp.EdgeCdpContext.launch(profile, viewport={"width": 1000, "height": 800}, accept_downloads=False)
    else:
        context = edge_cdp.EdgeCdpContext.launch(profile, viewport={"width": 1000, "height": 800}, accept_downloads=False)
        def fail_close():
            raise edge_cdp.EdgeCdpError("test lifecycle failure")
        monkeypatch.setattr(context, "_close_owned_browser", fail_close)
        with pytest.raises(edge_cdp.EdgeCdpError, match="test lifecycle failure"):
            context.close()
    lease = edge_cdp._acquire_profile_lease(profile)
    lease.close()


def test_cdp_socket_timeout_preserves_timeout_exception_type():
    class Socket:
        def send(self, payload):
            pass
        def settimeout(self, value):
            pass
        def recv(self):
            raise edge_cdp.websocket.WebSocketTimeoutException("synthetic stalled socket")

    connection = object.__new__(edge_cdp._CdpConnection)
    connection.closed = False
    connection._next_id = 1
    connection._pending = {}
    connection._socket = Socket()

    with pytest.raises(edge_cdp.EdgeCdpTimeout):
        connection.call("Runtime.evaluate", timeout_ms=2_000)


def test_cdp_unrelated_event_does_not_restart_the_receive_deadline(monkeypatch):
    clock = {"now": 0.0}
    limits = []
    class Socket:
        count = 0
        def send(self, payload):
            pass
        def settimeout(self, value):
            limits.append(value)
        def recv(self):
            self.count += 1
            if self.count == 1:
                clock["now"] += 0.8
                return '{"method":"Page.loadEventFired","params":{}}'
            clock["now"] += limits[-1]
            raise edge_cdp.websocket.WebSocketTimeoutException("synthetic delayed response")
    monkeypatch.setattr(edge_cdp.time, "monotonic", lambda: clock["now"])
    connection = object.__new__(edge_cdp._CdpConnection)
    connection.closed, connection._next_id, connection._pending = False, 1, {}
    connection._socket = Socket()
    with pytest.raises(edge_cdp.EdgeCdpTimeout):
        connection.call("Runtime.evaluate", timeout_ms=1000)
    assert clock["now"] <= 1.001
    assert limits[-1] == pytest.approx(0.2)


def test_stalled_browser_close_still_closes_connections_and_releases_lease(tmp_path, monkeypatch):
    cleanup = []

    class Connection:
        def call(self, *args, **kwargs):
            raise edge_cdp.EdgeCdpTimeout("synthetic stalled close")

    page = SimpleNamespace(_connection=Connection(), close_connection=lambda: cleanup.append("socket"))
    process = SimpleNamespace(wait=lambda **kwargs: cleanup.append("process"))
    context = edge_cdp.EdgeCdpContext(process, "http://127.0.0.1:25687", tmp_path)
    context._page_by_target["synthetic"] = page
    context._profile_lease = edge_cdp._acquire_profile_lease(tmp_path)
    def closed_endpoint(*args, **kwargs):
        raise edge_cdp.EdgeCdpError("synthetic endpoint closed")
    monkeypatch.setattr(edge_cdp, "_http_json", closed_endpoint)
    monkeypatch.setattr(edge_cdp.time, "sleep", lambda _: None)

    context.close()

    assert cleanup == ["socket", "process"]
    lease = edge_cdp._acquire_profile_lease(tmp_path)
    lease.close()


def test_stalled_failed_launch_close_still_releases_its_owned_process(monkeypatch):
    cleanup = []

    class Connection:
        def __init__(self, url):
            pass
        def call(self, *args, **kwargs):
            raise edge_cdp.EdgeCdpTimeout("synthetic stalled close")
        def close(self):
            cleanup.append("socket")

    process = SimpleNamespace(poll=lambda: None, terminate=lambda: cleanup.append("terminate"),
                              wait=lambda **kwargs: cleanup.append("process"))
    monkeypatch.setattr(edge_cdp, "_CdpConnection", Connection)

    edge_cdp._close_failed_browser(process, {"webSocketDebuggerUrl": "ws://127.0.0.1:25687/devtools/browser/synthetic"})

    assert cleanup == ["socket", "terminate", "process"]
