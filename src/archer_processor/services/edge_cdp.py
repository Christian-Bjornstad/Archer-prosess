from __future__ import annotations

import base64
import io
import math
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import websocket
from PIL import Image


class EdgeCdpError(RuntimeError):
    """Microsoft Edge or its local DevTools connection failed."""


class EdgeCdpTimeout(TimeoutError):
    """A CDP navigation or DOM wait exceeded its configured timeout."""


def find_edge_executable() -> Path:
    """Find the installed, organisation-managed Microsoft Edge executable."""
    discovered = shutil.which("msedge")
    candidates = [Path(discovered)] if discovered else []
    for environment_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(environment_name)
        if root:
            candidates.append(
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise EdgeCdpError(
        "Microsoft Edge was not found. Install or ask IT to expose the managed "
        "Edge installation before using browser evidence collection."
    )


def edge_cdp_available() -> bool:
    try:
        find_edge_executable()
        import websocket as _websocket  # noqa: F401
    except (ImportError, EdgeCdpError):
        return False
    return True


@contextmanager
def sync_edge_cdp() -> Iterator["EdgeCdpRuntime"]:
    runtime = EdgeCdpRuntime()
    try:
        yield runtime
    finally:
        runtime.close()


class EdgeCdpRuntime:
    def __init__(self) -> None:
        self.chromium = EdgeCdpLauncher(self)
        self._contexts: list[EdgeCdpContext] = []

    def close(self) -> None:
        for context in reversed(self._contexts):
            context.close()
        self._contexts.clear()


class EdgeCdpLauncher:
    def __init__(self, runtime: EdgeCdpRuntime) -> None:
        self.runtime = runtime

    def launch_persistent_context(
        self,
        user_data_dir: str,
        *,
        channel: str = "msedge",
        headless: bool = False,
        accept_downloads: bool = True,
        viewport: dict[str, int] | None = None,
        background: bool = False,
    ) -> "EdgeCdpContext":
        if channel not in {"msedge", "edge"}:
            raise EdgeCdpError(f"Unsupported CDP browser channel: {channel}")
        if headless:
            raise EdgeCdpError("Archer browser evidence requires visible Edge.")
        last_error: Exception | None = None
        context: EdgeCdpContext | None = None
        for attempt in range(1, 4):
            try:
                context = EdgeCdpContext.launch(
                    Path(user_data_dir),
                    viewport=viewport or {"width": 1440, "height": 1000},
                    accept_downloads=accept_downloads,
                    background=background,
                )
                break
            except EdgeCdpTimeout:
                raise
            except EdgeCdpError as exc:
                last_error = exc
                if attempt < 3:
                    # Managed Edge can briefly retain its process broker after
                    # the previous provider closes. Backing off prevents the
                    # next provider launch from being redirected to that stale
                    # broker without a DevTools endpoint.
                    time.sleep(attempt * 1.5)
        if context is None:
            raise EdgeCdpError(
                "Microsoft Edge could not open its dedicated evidence profile "
                f"after 3 attempts. Last error: {last_error}"
            ) from last_error
        self.runtime._contexts.append(context)
        return context


class EdgeCdpContext:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        endpoint: str,
        origin: str,
        profile_directory: Path,
    ) -> None:
        self.process = process
        self.endpoint = endpoint
        self.origin = origin
        self.profile_directory = profile_directory
        self._page_by_target: dict[str, EdgeCdpPage] = {}
        self._closed = False

    @classmethod
    def launch(
        cls,
        profile_directory: Path,
        *,
        viewport: dict[str, int],
        accept_downloads: bool,
        background: bool = False,
    ) -> "EdgeCdpContext":
        profile_directory.mkdir(parents=True, exist_ok=True)
        edge = find_edge_executable()
        port = _reserve_local_port()
        origin = f"http://127.0.0.1:{port}"
        endpoint = origin
        arguments = [
            str(edge),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-allow-origins={origin}",
            f"--user-data-dir={profile_directory.resolve()}",
            f"--window-size={int(viewport['width'])},{int(viewport['height'])}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--disable-background-mode",
            "--disable-features=msEdgeStartupBoost",
            "--disable-session-crashed-bubble",
            "about:blank",
        ]
        if background:
            arguments[1:1] = [
                "--start-minimized",
                "--window-position=-32000,-32000",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        started_at = time.monotonic()
        deadline = started_at + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                _http_json(f"{endpoint}/json/version", timeout=1)
                context = cls(process, endpoint, origin, profile_directory)
                pages = context.pages
                if not pages:
                    context.new_page()
                if accept_downloads and context.pages:
                    download_directory = profile_directory / "downloads"
                    download_directory.mkdir(parents=True, exist_ok=True)
                    try:
                        context.pages[0]._connection.call(
                            "Browser.setDownloadBehavior",
                            {
                                "behavior": "allow",
                                "downloadPath": str(download_directory),
                            },
                        )
                    except EdgeCdpError:
                        pass
                return context
            except Exception as exc:
                last_error = exc
                # Managed Edge may hand the command to its broker and let the
                # original process exit before the broker exposes CDP. Keep
                # probing the reserved endpoint briefly before calling that a
                # failed launch.
                if process.poll() is not None and time.monotonic() - started_at >= 3:
                    raise EdgeCdpError(
                        "Microsoft Edge exited before its local DevTools endpoint opened. "
                        "The dedicated browser profile may already be in use, or work-PC "
                        "policy may block remote debugging."
                    ) from last_error
                time.sleep(0.2)
        try:
            process.terminate()
        except OSError:
            pass
        raise EdgeCdpTimeout(
            "Microsoft Edge opened, but the local DevTools endpoint did not become "
            "available. Confirm that the Edge policy RemoteDebuggingAllowed is enabled."
        ) from last_error

    @property
    def pages(self) -> list["EdgeCdpPage"]:
        if self._closed:
            return []
        targets = _http_json(f"{self.endpoint}/json/list", timeout=2)
        page_targets = [item for item in targets if item.get("type") == "page"]
        active_ids = {str(item["id"]) for item in page_targets}
        for target_id in list(self._page_by_target):
            if target_id not in active_ids:
                self._page_by_target.pop(target_id).close_connection()
        pages: list[EdgeCdpPage] = []
        for target in page_targets:
            target_id = str(target["id"])
            page = self._page_by_target.get(target_id)
            if page is None:
                page = EdgeCdpPage(
                    target_id,
                    str(target["webSocketDebuggerUrl"]),
                    self.origin,
                )
                self._page_by_target[target_id] = page
            pages.append(page)
        return pages

    def new_page(self) -> "EdgeCdpPage":
        target = _http_json(
            f"{self.endpoint}/json/new?about:blank",
            method="PUT",
            timeout=3,
        )
        page = EdgeCdpPage(
            str(target["id"]),
            str(target["webSocketDebuggerUrl"]),
            self.origin,
        )
        self._page_by_target[page.target_id] = page
        return page

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pages = list(self._page_by_target.values())
        if pages:
            try:
                pages[0]._connection.call("Browser.close", timeout_ms=2_000)
            except EdgeCdpError:
                pass
        for page in pages:
            page.close_connection()
        self._page_by_target.clear()
        shutdown_deadline = time.monotonic() + 8
        while time.monotonic() < shutdown_deadline:
            try:
                _http_json(f"{self.endpoint}/json/version", timeout=0.25)
            except EdgeCdpError:
                break
            time.sleep(0.1)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        # Edge closes its DevTools socket before every profile database handle
        # has drained. A short release window prevents a subsequent patient or
        # login run from seeing a false "profile in use" error.
        time.sleep(0.5)


class _Dialog:
    def __init__(self, dialog_type: str) -> None:
        self.type = dialog_type
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True

    def dismiss(self) -> None:
        self.accepted = False


class _CdpConnection:
    def __init__(self, websocket_url: str, origin: str) -> None:
        try:
            self._socket = websocket.create_connection(
                websocket_url,
                timeout=10,
                origin=origin,
                enable_multithread=False,
            )
        except Exception as exc:
            raise EdgeCdpError(f"Could not connect to Microsoft Edge CDP: {exc}") from exc
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self.dialog_handler: Callable[[_Dialog], None] | None = None
        self.closed = False

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        if self.closed:
            raise EdgeCdpError("The Microsoft Edge CDP connection is closed.")
        command_id = self._next_id
        self._next_id += 1
        payload = {"id": command_id, "method": method}
        if params:
            payload["params"] = params
        try:
            self._socket.send(json.dumps(payload))
            self._socket.settimeout(max(0.1, timeout_ms / 1_000))
            deadline = time.monotonic() + timeout_ms / 1_000
            while time.monotonic() < deadline:
                pending = self._pending.pop(command_id, None)
                if pending is not None:
                    return self._result(method, pending)
                try:
                    message = json.loads(self._socket.recv())
                except websocket.WebSocketTimeoutException as exc:
                    raise EdgeCdpTimeout(f"Microsoft Edge timed out during {method}.") from exc
                if "id" in message:
                    response_id = int(message["id"])
                    if response_id == command_id:
                        return self._result(method, message)
                    self._pending[response_id] = message
                else:
                    self._handle_event(message)
        except EdgeCdpError:
            raise
        except Exception as exc:
            raise EdgeCdpError(f"Microsoft Edge CDP failed during {method}: {exc}") from exc
        raise EdgeCdpTimeout(f"Microsoft Edge timed out during {method}.")

    @staticmethod
    def _result(method: str, message: dict[str, Any]) -> dict[str, Any]:
        if "error" in message:
            error = message["error"]
            raise EdgeCdpError(
                f"Microsoft Edge rejected {method}: {error.get('message', error)}"
            )
        return dict(message.get("result") or {})

    def _handle_event(self, message: dict[str, Any]) -> None:
        if message.get("method") != "Page.javascriptDialogOpening":
            return
        dialog = _Dialog(str(message.get("params", {}).get("type", "alert")))
        handler = self.dialog_handler
        self.dialog_handler = None
        if handler is not None:
            handler(dialog)
        command_id = self._next_id
        self._next_id += 1
        self._socket.send(
            json.dumps(
                {
                    "id": command_id,
                    "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": dialog.accepted},
                }
            )
        )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._socket.close()
        except Exception:
            pass


class EdgeCdpPage:
    def __init__(self, target_id: str, websocket_url: str, origin: str) -> None:
        self.target_id = target_id
        self._connection = _CdpConnection(websocket_url, origin)
        self._last_url = "about:blank"
        self._connection.call("Page.enable")
        self._connection.call("Runtime.enable")

    @property
    def url(self) -> str:
        try:
            value = self._evaluate_value("location.href", timeout_ms=2_000)
            if value:
                self._last_url = str(value)
        except EdgeCdpError:
            pass
        return self._last_url

    def goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 30_000,
    ) -> None:
        response = self._connection.call(
            "Page.navigate", {"url": url}, timeout_ms=timeout
        )
        if response.get("errorText"):
            raise EdgeCdpError(f"Edge navigation failed: {response['errorText']}")
        self._last_url = url
        required_states = {"interactive", "complete"}
        if wait_until == "load":
            required_states = {"complete"}
        deadline = time.monotonic() + timeout / 1_000
        while time.monotonic() < deadline:
            try:
                state = self._evaluate_value(
                    "document.readyState", timeout_ms=min(2_000, timeout)
                )
                if state in required_states:
                    self._last_url = str(
                        self._evaluate_value("location.href", timeout_ms=2_000) or url
                    )
                    return
            except EdgeCdpError:
                pass
            time.sleep(0.05)
        raise EdgeCdpTimeout(f"Edge did not finish navigating to {url}.")

    def bring_to_front(self) -> None:
        self._connection.call("Page.bringToFront")

    def wait_for_timeout(self, milliseconds: int) -> None:
        time.sleep(max(0, milliseconds) / 1_000)

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        deadline = time.monotonic() + timeout / 1_000
        stable_since: float | None = None
        while time.monotonic() < deadline:
            ready = self._evaluate_value("document.readyState", timeout_ms=2_000)
            if state == "networkidle":
                if ready == "complete":
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= 0.5:
                        return
                else:
                    stable_since = None
            elif ready in {"interactive", "complete"}:
                return
            time.sleep(0.1)
        raise EdgeCdpTimeout(f"Edge did not reach load state {state!r}.")

    def wait_for_url(
        self,
        pattern: re.Pattern[str] | str | Callable[[str], bool],
        *,
        timeout: int,
    ) -> None:
        predicate: Callable[[str], bool]
        if callable(pattern) and not isinstance(pattern, re.Pattern):
            predicate = pattern
            description = getattr(pattern, "__name__", "URL predicate")
        else:
            compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
            predicate = lambda url: bool(compiled.fullmatch(url))
            description = compiled.pattern
        deadline = time.monotonic() + timeout / 1_000
        while time.monotonic() < deadline:
            if predicate(self.url):
                return
            time.sleep(0.1)
        raise EdgeCdpTimeout(f"Edge URL did not match {description!r}.")

    def locator(self, selector: str) -> "EdgeCdpLocator":
        return EdgeCdpLocator(self, _css_expression(selector))

    def get_by_role(
        self,
        role: str,
        *,
        name: str | None = None,
        exact: bool = False,
    ) -> "EdgeCdpLocator":
        return EdgeCdpLocator(self, _role_expression(role, name, exact))

    def get_by_text(self, text: str, *, exact: bool = False) -> "EdgeCdpLocator":
        return EdgeCdpLocator(self, _text_expression(text, exact))

    def evaluate(self, script: str) -> Any:
        return self._evaluate_value(script)

    def once(self, event: str, handler: Callable[[_Dialog], None]) -> None:
        if event != "dialog":
            raise EdgeCdpError(f"Unsupported one-shot Edge event: {event}")
        self._connection.dialog_handler = handler

    def screenshot(
        self,
        *,
        path: str,
        full_page: bool = False,
        clip: dict[str, float] | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "format": "png",
            "captureBeyondViewport": bool(full_page or clip),
            "fromSurface": True,
        }
        if full_page or clip:
            metrics = self._connection.call("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": max(1, float(size.get("width", 1))),
                "height": max(1, float(size.get("height", 1))),
                "scale": 1,
            }
        if clip:
            scroll = self._evaluate_value(
                "({x: window.scrollX, y: window.scrollY})"
            ) or {"x": 0, "y": 0}
            crop_box = {
                "x": float(clip["x"]) + float(scroll.get("x", 0)),
                "y": float(clip["y"]) + float(scroll.get("y", 0)),
                "width": max(1, float(clip["width"])),
                "height": max(1, float(clip["height"])),
                "scale": 1,
            }
        result = self._connection.call("Page.captureScreenshot", params)
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(result["data"])
        if clip:
            # Keep the browser capture at document origin, then account for
            # browser zoom / device scaling using actual image dimensions.
            with Image.open(io.BytesIO(data)) as source:
                sx = source.width / params["clip"]["width"]
                sy = source.height / params["clip"]["height"]
                left = max(0, math.floor(crop_box["x"] * sx))
                top = max(0, math.floor(crop_box["y"] * sy))
                right = min(source.width, math.ceil((crop_box["x"] + crop_box["width"]) * sx))
                bottom = min(source.height, math.ceil((crop_box["y"] + crop_box["height"]) * sy))
                if right <= left or bottom <= top:
                    raise EdgeCdpError("Screenshot target lies outside the document.")
                source.crop((left, top, right, bottom)).save(output)
        else:
            output.write_bytes(data)

    def _evaluate_value(self, expression: str, *, timeout_ms: int = 30_000) -> Any:
        result = self._connection.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout_ms=timeout_ms,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            exception = detail.get("exception") or {}
            message = (
                exception.get("description")
                or exception.get("value")
                or detail.get("text")
                or "unknown error"
            )
            raise EdgeCdpError(
                f"Edge JavaScript failed: {message}"
            )
        return result.get("result", {}).get("value")

    def close_connection(self) -> None:
        self._connection.close()


class EdgeCdpLocator:
    def __init__(self, page: EdgeCdpPage, expression: str) -> None:
        self.page = page
        self.expression = expression

    @property
    def first(self) -> "EdgeCdpLocator":
        return EdgeCdpLocator(self.page, f"({self.expression}).slice(0, 1)")

    def nth(self, index: int) -> "EdgeCdpLocator":
        return EdgeCdpLocator(
            self.page,
            f"({self.expression}).slice({int(index)}, {int(index) + 1})",
        )

    def locator(self, selector: str) -> "EdgeCdpLocator":
        if selector.startswith("xpath="):
            xpath = json.dumps(selector.removeprefix("xpath="))
            expression = (
                "[...new Set((" + self.expression + ").map(root => "
                f"document.evaluate({xpath}, root, null, "
                "XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue"
                ").filter(Boolean))]"
            )
        else:
            css = json.dumps(selector)
            expression = (
                "[...new Set((" + self.expression + ").flatMap(root => "
                f"[...root.querySelectorAll({css})]))]"
            )
        return EdgeCdpLocator(self.page, expression)

    def filter(
        self,
        *,
        has: "EdgeCdpLocator" | None = None,
        has_text: re.Pattern[str] | str | None = None,
    ) -> "EdgeCdpLocator":
        expression = self.expression
        if has is not None:
            expression = (
                f"({expression}).filter(root => ({has.expression})"
                ".some(child => root !== child && root.contains(child)))"
            )
        if has_text is not None:
            if isinstance(has_text, re.Pattern):
                flags = ""
                if has_text.flags & re.IGNORECASE:
                    flags += "i"
                if has_text.flags & re.MULTILINE:
                    flags += "m"
                if has_text.flags & re.DOTALL:
                    flags += "s"
                source = has_text.pattern
            else:
                flags = ""
                source = str(has_text)
            expression = (
                f"({expression}).filter(el => new RegExp({json.dumps(source)}, "
                f"{json.dumps(flags)}).test((el.innerText || '').trim()))"
            )
        return EdgeCdpLocator(self.page, expression)

    def count(self) -> int:
        return int(self.page._evaluate_value(f"({self.expression}).length") or 0)

    def wait_for(self, *, state: str = "visible", timeout: int = 30_000) -> None:
        deadline = time.monotonic() + timeout / 1_000
        while time.monotonic() < deadline:
            count = self.count()
            if state == "detached" and count == 0:
                return
            if state == "attached" and count > 0:
                return
            if state == "visible" and count > 0 and self.is_visible():
                return
            time.sleep(0.1)
        raise EdgeCdpTimeout(f"Edge element did not reach state {state!r}.")

    def is_visible(self) -> bool:
        return bool(
            self.page._evaluate_value(
                "(() => { const nodes = "
                + self.expression
                + "; if (nodes.length !== 1) return false; const el = nodes[0]; "
                "const style = getComputedStyle(el); const rect = el.getBoundingClientRect(); "
                "return style.visibility !== 'hidden' && style.display !== 'none' "
                "&& rect.width > 0 && rect.height > 0; })()"
            )
        )

    def click(self) -> None:
        clicked = self.page._evaluate_value(
            "(() => { const nodes = "
            + self.expression
            + "; if (nodes.length !== 1) throw new Error(`Expected one element, got ${nodes.length}`); "
            "const el = nodes[0]; el.scrollIntoView({block:'center', inline:'center'}); "
            "el.focus(); el.click(); return true; })()"
        )
        if not clicked:
            raise EdgeCdpError("Edge could not click the selected element.")

    def click_physical(self) -> None:
        """Dispatch a trusted pointer click for custom controls that ignore el.click()."""
        point = self.page._evaluate_value(
            "(() => { const nodes = "
            + self.expression
            + "; if (nodes.length !== 1) throw new Error(`Expected one element, got ${nodes.length}`); "
            "const el=nodes[0]; el.scrollIntoView({block:'center',inline:'center'}); "
            "const r=el.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2}; })()"
        )
        if not point:
            raise EdgeCdpError("Edge could not calculate the physical click position.")
        for event_type in ("mousePressed", "mouseReleased"):
            self.page._connection.call(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": float(point["x"]),
                    "y": float(point["y"]),
                    "button": "left",
                    "clickCount": 1,
                },
            )

    def fill(self, value: str) -> None:
        argument = json.dumps(value)
        self.page._evaluate_value(
            "(() => { const nodes = "
            + self.expression
            + "; if (nodes.length !== 1) throw new Error(`Expected one element, got ${nodes.length}`); "
            f"const el=nodes[0], value={argument}; el.focus(); "
            "const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : "
            "el instanceof HTMLInputElement ? HTMLInputElement.prototype : null; "
            "const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value')?.set; "
            "if (setter) setter.call(el, value); else el.value=value; "
            "el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:value})); "
            "el.dispatchEvent(new Event('change',{bubbles:true})); return true; })()"
        )

    def press(self, key: str) -> None:
        if key != "Enter":
            raise EdgeCdpError(f"Unsupported Edge key press: {key}")
        self.page._evaluate_value(
            "(() => { const nodes=" + self.expression + "; "
            "if(nodes.length!==1) throw new Error(`Expected one element, got ${nodes.length}`); "
            "nodes[0].focus(); return true; })()"
        )
        self.page._connection.call(
            "Input.dispatchKeyEvent",
            {
                "type": "rawKeyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            },
        )
        self.page._connection.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            },
        )

    def inner_text(self, timeout: int = 30_000) -> str:
        self.wait_for(state="attached", timeout=timeout)
        value = self.page._evaluate_value(
            "(() => { const nodes=" + self.expression + "; "
            "if(nodes.length!==1) throw new Error(`Expected one element, got ${nodes.length}`); "
            "return nodes[0].innerText || ''; })()"
        )
        return str(value or "")

    def all_inner_texts(self) -> list[str]:
        values = self.page._evaluate_value(
            f"({self.expression}).map(el => el.innerText || '')"
        )
        return [str(value) for value in values or []]

    def get_attribute(self, name: str) -> str | None:
        value = self.page._evaluate_value(
            "(() => { const nodes=" + self.expression + "; "
            "if(nodes.length!==1) throw new Error(`Expected one element, got ${nodes.length}`); "
            f"return nodes[0].getAttribute({json.dumps(name)}); }})()"
        )
        return None if value is None else str(value)

    def bounding_box(self) -> dict[str, float] | None:
        value = self.page._evaluate_value(
            "(() => { const nodes=" + self.expression + "; if(nodes.length!==1) return null; "
            "const r=nodes[0].getBoundingClientRect(); if(!r.width || !r.height) return null; "
            "return {x:r.x,y:r.y,width:r.width,height:r.height}; })()"
        )
        return dict(value) if value else None

    def scroll_into_view_if_needed(self) -> None:
        """Bring one visible locator into the viewport before interaction/capture."""
        scrolled = self.page._evaluate_value(
            "(() => { const nodes=" + self.expression + "; "
            "if(nodes.length!==1) throw new Error(`Expected one element, got ${nodes.length}`); "
            "const el=nodes[0], style=getComputedStyle(el), rect=el.getBoundingClientRect(); "
            "if(style.visibility==='hidden' || style.display==='none' || !rect.width || !rect.height) "
            "throw new Error('Element is hidden'); "
            "if(rect.top < 0 || rect.left < 0 || rect.bottom > innerHeight || rect.right > innerWidth) "
            "el.scrollIntoView({block:'center',inline:'nearest'}); return true; })()"
        )
        if not scrolled:
            raise EdgeCdpError("Edge could not scroll the selected element into view.")

    def evaluate(self, script: str, argument: Any = None) -> Any:
        return self.page._evaluate_value(
            "(() => { const nodes=" + self.expression + "; "
            "if(nodes.length!==1) throw new Error(`Expected one element, got ${nodes.length}`); "
            f"return ({script})(nodes[0], {json.dumps(argument)}); }})()"
        )

    def evaluate_all(self, script: str) -> Any:
        return self.page._evaluate_value(f"({script})({self.expression})")

    def screenshot(self, *, path: str) -> None:
        box = self.bounding_box()
        if box is None:
            raise EdgeCdpError("Edge cannot screenshot a hidden or missing element.")
        self.page.screenshot(path=path, clip=box)


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_json(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 3,
) -> Any:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise EdgeCdpError(f"Edge DevTools endpoint failed: {url}: {exc}") from exc


def _css_expression(selector: str) -> str:
    if selector.startswith("xpath="):
        raise EdgeCdpError("A page-level XPath locator is not supported.")
    return f"[...document.querySelectorAll({json.dumps(selector)})]"


def _role_expression(role: str, name: str | None, exact: bool) -> str:
    selectors = {
        "heading": "h1,h2,h3,h4,h5,h6,[role='heading']",
        "link": "a[href],[role='link']",
        "button": "button,input[type='button'],input[type='submit'],[role='button']",
        "treeitem": "[role='treeitem']",
        "combobox": "select,[role='combobox'],input[list]",
        "option": "option,[role='option']",
        "tab": "[role='tab']",
        "textbox": (
            "textarea,input:not([type]),input[type='text'],input[type='email'],"
            "input[type='password'],input[type='search'],[role='textbox']"
        ),
    }
    selector = selectors.get(role, f"[role={json.dumps(role)}]")
    base = f"[...document.querySelectorAll({json.dumps(selector)})]"
    if name is None:
        return base
    expected = json.dumps(name)
    comparison = "actual === expected" if exact else "actual.includes(expected)"
    return (
        "(() => { const norm=v=>(v||'').replace(/\\s+/g,' ').trim(); "
        f"const expected=norm({expected}); return ({base}).filter(el => {{ "
        "const labelled=el.getAttribute('aria-labelledby'); "
        "const labelText=labelled ? labelled.split(/\\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ') : ''; "
        "const actual=norm(el.getAttribute('aria-label') || labelText || el.getAttribute('placeholder') || "
        "el.getAttribute('alt') || el.innerText || el.value || ''); "
        f"return {comparison}; }}); }})()"
    )


def _text_expression(text: str, exact: bool) -> str:
    expected = json.dumps(text)
    comparison = "actual === expected" if exact else "actual.includes(expected)"
    return (
        "(() => { const norm=v=>(v||'').replace(/\\s+/g,' ').trim(); "
        f"const expected=norm({expected}); const matches=el => {{ const actual=norm(el.innerText || ''); "
        f"return !!actual && {comparison}; }}; return [...document.querySelectorAll('body *')]"
        ".filter(el => matches(el) && ![...el.children].some(child => matches(child))); })()"
    )
