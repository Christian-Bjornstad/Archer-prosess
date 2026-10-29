"""Check and restore evidence-site sessions in Archer's dedicated Edge profiles."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from archer_processor.services.browser_review import BROWSER_DATABASES, BrowserAutomationUnavailable, BrowserReviewService


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
    searches. A provider login form triggers one attempt with saved credentials,
    followed by verification of the actual page. No evidence queries, reports,
    downloads, or captures are created.
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
            result = self.check(database)
            results.append(result)
            if on_result is not None:
                on_result(result)
        return results

    def check(self, database: str) -> BrowserSessionStatus:
        """Check one provider, restoring its session with saved credentials if needed.

        Unknown providers raise ValueError before opening any browser. Runtime
        failures and cancellation use the same status contract as check_all.
        """
        if database not in BROWSER_DATABASES:
            raise ValueError(f"Unknown evidence provider: {database!r}")
        if self.review_service.stop_requested():
            return BrowserSessionStatus(database, "unknown", "Session check cancelled.")
        return self._check_provider(database)

    def _check_provider(self, database: str) -> BrowserSessionStatus:
        review = self.review_service
        try:
            sync_browser, _, _ = review._browser_api()
            with sync_browser() as runtime:
                context = review._launch_browser_context(
                    runtime, database,
                    accept_downloads=False,
                    background=review.browser_background,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(
                        review.login_url(database), wait_until="domcontentloaded",
                        timeout=max(1, min(15_000, review.navigation_timeout_ms)),
                    )
                    result = self._settle_page(database, page)
                    # Only a visible form on the provider's trusted HTTPS host
                    # can trigger credential entry. A redirect or public page cannot.
                    if database == "ClinVar" or result.status != "login_required":
                        return result
                    prefix = database.lower()
                    if not (
                        getattr(review, f"{prefix}_email")
                        and getattr(review, f"{prefix}_password")
                    ):
                        return BrowserSessionStatus(
                            database, "login_required",
                            "Ikke innlogget. Lagre brukernavn og passord under Innstillinger, "
                            "eller bruk Logg inn for manuell innlogging.",
                        )
                    if review.stop_requested():
                        return BrowserSessionStatus(database, "unknown", "Session check cancelled.")
                    review._try_saved_login(database, page)
                    # A successful submission/redirect is not proof of sign-in.
                    result = self._settle_page(database, page, after_login=True)
                    if result.status == "login_required":
                        return BrowserSessionStatus(
                            database, "login_required",
                            "Innlogging med lagrede opplysninger ble ikke bekreftet. "
                            "Kontroller opplysningene i Innstillinger eller bruk Logg inn "
                            "for eventuelle ekstra innloggingssteg.",
                        )
                    return result
                finally:
                    context.close()
        except Exception as exc:
            # Do not include redirected URLs, DOM contents, or credentials.
            return BrowserSessionStatus(
                database, "error",
                f"Could not check {database}: {type(exc).__name__}. {self._error_guidance(exc)}",
            )

    def _settle_page(self, database: str, page: Any, *, after_login: bool = False) -> BrowserSessionStatus:
        deadline = time.monotonic() + self.settle_timeout_ms / 1_000
        pending = {"unknown", "login_required"} if after_login else {"unknown"}
        while True:
            if self.review_service.stop_requested():
                return BrowserSessionStatus(database, "unknown", "Session check cancelled.")
            result = self.inspect_page(database, page)
            if result.status not in pending or time.monotonic() >= deadline:
                return result
            page.wait_for_timeout(250)

    @staticmethod
    def _error_guidance(exc: Exception) -> str:
        # Only startup diagnostics precede credential entry. Runtime DOM/CDP
        # errors can contain submitted values or redirected authentication URLs.
        if isinstance(exc, BrowserAutomationUnavailable):
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
