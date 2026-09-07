"""Local-only Edge startup diagnostics. Never opens a database or an existing profile."""
from datetime import datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request


def probe(port, method):
    if method == "TCP":
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return "connected"
    if method == "HTTP direct":
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            data = response.read()
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
        finally:
            connection.close()
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            data = response.read()
    result = json.loads(data)
    return str(result.get("Browser", "local test server"))


class LocalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = b'{"Browser":"local test server"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main(fixed_port=False):
    directory = Path(tempfile.mkdtemp(prefix="vpm-edge-diagnostic-"))
    report = directory / "diagnostic.txt"

    def log(message):
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        print(line, flush=True)
        with report.open("a", encoding="utf-8") as output:
            output.write(line + "\n")

    def check(port, label):
        outcomes = []
        for method in ("TCP", "HTTP direct", "HTTP urllib (no proxy)"):
            try:
                detail = probe(port, method)
                outcomes.append(True)
                log(f"{label} {method}: OK ({detail})")
            except Exception as exc:
                outcomes.append(False)
                log(f"{label} {method}: FAIL {type(exc).__name__}: {exc}")
        return all(outcomes)

    log("VPM local connection diagnostic; existing profiles are NOT modified.")
    log(f"Mode: {'fixed port (like app)' if fixed_port else 'Edge-selected port'}")
    log(f"OS: {platform.platform()}; Python: {sys.version.split()[0]}; executable: {sys.executable}")
    log("Proxy environment variables present (values not collected): " +
        ", ".join(key for key in os.environ if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}))
    log(f"Report: {report}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        check(server.server_port, "Python loopback baseline")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    candidates = [Path(os.environ.get(key, "")) / "Microsoft/Edge/Application/msedge.exe"
                  for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    edge = next((path for path in candidates if path.is_file()), None)
    if edge is None:
        log("Edge executable not found.")
        return report
    profile = directory / "isolated-profile"
    # Let Edge select its port and announce it in DevToolsActivePort. This also
    # lets us compare actual listening state with the app's fixed-port startup.
    requested_port = 0
    if fixed_port:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            requested_port = reservation.getsockname()[1]
    arguments = [str(edge), f"--remote-debugging-port={requested_port}", "--remote-debugging-address=127.0.0.1",
                 f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                 "--disable-background-mode", "--disable-features=msEdgeStartupBoost", "about:blank"]
    log(f"Fresh diagnostic profile: {profile}")
    log(f"Requested port: {requested_port}; waiting up to 60 seconds; no website will be queried.")
    process = None
    try:
        with (directory / "edge-startup.log").open("ab") as startup:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=startup, stderr=startup)
        started = time.monotonic()
        deadline = started + 60
        port = requested_port or None
        while time.monotonic() < deadline:
            announcement = profile / "DevToolsActivePort"
            if port is None and announcement.exists():
                try:
                    port = int(announcement.read_text().splitlines()[0])
                    if not 1 <= port <= 65535:
                        raise ValueError("Invalid port")
                    log(f"Edge announced 127.0.0.1:{port} after {time.monotonic()-started:.1f}s")
                except (OSError, ValueError, IndexError):
                    port = None
            if port is not None and check(port, "Edge"):
                log(f"PASS: fresh-profile Edge is reachable after {time.monotonic()-started:.1f}s.")
                break
            if process.poll() is not None:
                log(f"Edge launcher exited: {process.returncode}; still checking for broker startup.")
            time.sleep(3)
        else:
            log("FAIL: Edge connection did not pass within the diagnostic window.")
        log("Compare with the app: baseline failures suggest local networking; Edge-only failures require Edge/session investigation.")
    except Exception as exc:
        log(f"Diagnostic error: {type(exc).__name__}: {exc}")
    finally:
        # Only terminate the exact diagnostic process we created; never taskkill
        # all Edge processes, delete profiles, or touch the user's regular browser.
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log("Diagnostic Edge still running; close its blank window manually.")
        log("Diagnostic files retained. Existing app/browser profiles unchanged.")
        log("Edge startup log follows (new diagnostic profile only):")
        startup_path = directory / "edge-startup.log"
        if startup_path.exists():
            log(startup_path.read_text(encoding="utf-8", errors="replace")[-16000:])
    return report


if __name__ == "__main__":
    reports = [main(fixed_port=True), main(fixed_port=False)]
    if os.name == "nt" and os.environ.get("VPM_DIAGNOSTIC_NO_VIEW") != "1":
        for diagnostic_report in reports:
            subprocess.Popen(["notepad.exe", str(diagnostic_report)])
