"""Check evidence-site sessions using only Archer's dedicated Edge profiles."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from archer_processor.services.browser_review import BROWSER_DATABASES, BrowserReviewService


SessionState = Literal["authenticated", "public", "login_required", "unknown", "error"]

_SESSION_PROBE = r"""() => {
    const visible = el => {
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
    };
    const has = selector => [...document.querySelectorAll(selector)].some(visible);
    const signOut = [...document.querySelectorAll('a[href],button,[role="link"],[role="button"]')]
        .some(el => visible(el) && /^\s*(?:log\s*out|sign\s*out)\s*$/i.test(el.innerText || el.getAttribute('aria-label') || ''));
    const body = document.body?.innerText || '';
    return {
        url: location.href,
        login_form: has('input[type="password"],#password'),
        sign_out: signOut,
        mtbp_form: has('#variant-input'),
        franklin_search: has("input[placeholder='Enter variant, gene or select an example above']"),
        clinvar_page: [...document.querySelectorAll('h1,h2,[role="heading"]')]
            .some(el => visible(el) && /\bClinVar\b/.test(el.innerText || '')),
        access_error: /access denied|ERR_[A-Z_]+|proxy authentication|required.*certificate|site can.t be reached/i.test(body)
    };
}"""


@dataclass(frozen=True)
class BrowserSessionStatus:
    database: str
    status: SessionState
    message: str


class BrowserSessionCheckService:
    """Blocking worker API; the GUI must exclude searches and login workers.

    Each provider runs sequentially in the same profile used by evidence
    searches. This checks existing cookies only: it never fills credentials,
    submits queries, creates reports, downloads files, or captures evidence.
    `on_result` runs on the calling worker thread, once per provider.
    """

    def __init__(self, review_service: BrowserReviewService, *, settle_timeout_ms: int = 3_000) -> None:
        self.review_service = review_service
        self.settle_timeout_ms = max(0, min(5_000, int(settle_timeout_ms)))

    def check_all(
        self, *, on_result: Callable[[BrowserSessionStatus], None] | None = None,
    ) -> list[BrowserSessionStatus]:
        results = []
        for database in BROWSER_DATABASES:
            if self.review_service.stop_requested():
                result = BrowserSessionStatus(database, "unknown", "Session check cancelled.")
            else:
                result = self._check_provider(database)
            results.append(result)
            if on_result is not None:
                on_result(result)
        return results

    def _check_provider(self, database: str) -> BrowserSessionStatus:
        review = self.review_service
        try:
            sync_browser, _, _ = review._browser_api()
            with sync_browser() as runtime:
                context = runtime.chromium.launch_persistent_context(
                    str(review.profile_directory(database)),
                    channel=review.channel,
                    headless=False,
                    accept_downloads=False,
                    background=review.browser_background,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(
                        review.login_url(database), wait_until="domcontentloaded",
                        timeout=max(1, min(15_000, review.navigation_timeout_ms)),
                    )
                    deadline = time.monotonic() + self.settle_timeout_ms / 1_000
                    while True:
                        if review.stop_requested():
                            return BrowserSessionStatus(database, "unknown", "Session check cancelled.")
                        result = self.inspect_page(database, page)
                        if result.status != "unknown" or time.monotonic() >= deadline:
                            return result
                        page.wait_for_timeout(250)
                finally:
                    context.close()
        except Exception as exc:
            # Do not include redirected URLs, DOM contents, or credentials.
            return BrowserSessionStatus(
                database, "error",
                f"Could not check {database}: {type(exc).__name__}. {self._error_guidance(exc)}",
            )

    @staticmethod
    def _error_guidance(exc: Exception) -> str:
        from archer_processor.services.edge_cdp import EdgeCdpError, EdgeCdpTimeout

        if isinstance(exc, (EdgeCdpError, EdgeCdpTimeout)):
            return str(exc)
        return (
            "Check network access and the dedicated Edge profile. If Edge cannot "
            "start on this work PC, ask IT to inspect RemoteDebuggingAllowed and "
            "UserDataDir. Existing browser windows should remain open."
        )

    def inspect_page(self, database: str, page: Any) -> BrowserSessionStatus:
        """Require visible, provider-specific evidence before confirming access."""
        review = self.review_service
        review.login_url(database)  # Validate the provider before reading a page.
        snapshot = page.evaluate(_SESSION_PROBE, timeout_ms=2_000)
        boolean_fields = ("login_form", "sign_out", "mtbp_form", "franklin_search", "clinvar_page", "access_error")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("url"), str)
            or any(type(snapshot.get(name)) is not bool for name in boolean_fields)
        ):
            return BrowserSessionStatus(database, "unknown", "The page did not return a valid session check.")
        parsed = urlsplit(snapshot["url"])
        expected_host = urlsplit(review.login_url(database)).hostname
        provider_host = parsed.scheme == "https" and parsed.hostname == expected_host
        # MTBP's identity service lives on a separate first-party subdomain.
        # An unrelated redirect/captive portal is never this provider's login.
        auth_host = provider_host or (
            database == "MTBP" and parsed.scheme == "https"
            and bool(parsed.hostname and parsed.hostname.endswith(".mtbp.org"))
        )
        if snapshot["login_form"] and auth_host:
            return BrowserSessionStatus(database, "login_required", "Sign in using this site's login button, then check again.")
        if not provider_host or review._login_required(database, snapshot["url"]):
            return BrowserSessionStatus(database, "unknown", "The page has not confirmed access; it may still be loading or redirecting.")
        if snapshot["access_error"]:
            return BrowserSessionStatus(database, "error", "The site returned an access or network error. Check work-PC access with IT.")
        if database == "ClinVar":
            if parsed.path.rstrip("/") == "/clinvar" and snapshot["clinvar_page"]:
                return BrowserSessionStatus(database, "public", "ClinVar is public and does not require sign-in for evidence searches.")
        elif database == "MTBP" and parsed.path.rstrip("/") == "/analyse" and snapshot["mtbp_form"]:
            return BrowserSessionStatus(database, "authenticated", "Signed-in MTBP analysis form is available.")
        elif database == "Franklin" and parsed.path.startswith("/clinical-db/") and snapshot["franklin_search"]:
            return BrowserSessionStatus(database, "authenticated", "Signed-in Franklin search form is available.")
        elif database in {"COSMIC", "OncoKB"} and snapshot["sign_out"]:
            return BrowserSessionStatus(database, "authenticated", f"Signed-in {database} session is visible.")
        return BrowserSessionStatus(database, "unknown", "No signed-in page marker was found. Use the site's login button if access is needed.")
