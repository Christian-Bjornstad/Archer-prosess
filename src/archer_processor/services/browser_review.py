from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.evidence_audit import persist_evidence_result
from archer_processor.services.browser_popups import dismiss_known_overlays
from archer_processor.services.capture_validation import (
    CaptureValidation,
    IncompleteCaptureError,
    validate_capture,
)
from archer_processor.services.variant_identity import (
    GenomicIdentity,
    IdentityVerification,
    genomic_identity,
)


BROWSER_DATABASES = ("COSMIC", "OncoKB", "Franklin", "ClinVar", "MTBP")
MTBP_REPORTS_URL = "https://mtbp.org/patients/"
MTBP_REPORT_LIMIT = 10
MTBP_ARCHER_CLEANUP_THRESHOLD = 6

LOGIN_URLS = {
    "ClinVar": "https://www.ncbi.nlm.nih.gov/clinvar/",
    "COSMIC": "https://cancer.sanger.ac.uk/cosmic/login",
    "OncoKB": "https://www.oncokb.org/login",
    "Franklin": "https://franklin.genoox.com/login",
    "MTBP": "https://mtbp.org/analyse/",
}

FRANKLIN_HOME_URL = "https://franklin.genoox.com/clinical-db/home"
PROVIDER_SWITCH_DELAY_MS = 3_000

_AMINO_ACID_3_TO_1 = {
    "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C",
    "Gln": "Q", "Glu": "E", "Gly": "G", "His": "H", "Ile": "I",
    "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F", "Pro": "P",
    "Ser": "S", "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V",
    "Ter": "*",
}


class BrowserAutomationUnavailable(RuntimeError):
    """The optional visible-browser runtime is not installed or cannot start."""


class BrowserReviewCancelled(BaseException):
    """A cooperative browser review cancellation requested by the user."""


class MtbpReportTimeout(TimeoutError):
    """MTBP accepted a batch but did not publish its report before the deadline."""


class BrowserReviewService:
    """Run serial, visible website reviews in isolated persistent Edge profiles.

    Passwords are entered directly into each provider's page. The application
    retains only the provider browser profile (cookies/local storage), never the
    password itself. Browser sources are deliberately serial and separate from
    the HTTP/API search service.
    """

    def __init__(
        self,
        profile_root: Path | None = None,
        *,
        channel: str = "msedge",
        navigation_timeout_ms: int = 45_000,
        analysis_timeout_ms: int = 1_200_000,
        mtbp_cancer_type: str = "Blood",
        clinvar_api_key: str = "",
        cosmic_email: str = "",
        cosmic_password: str = "",
        oncokb_email: str = "",
        oncokb_password: str = "",
        franklin_email: str = "",
        franklin_password: str = "",
        mtbp_email: str = "",
        mtbp_password: str = "",
        franklin_attempts: int = 3,
        request_delay_ms: int = 10_000,
        request_delay_max_ms: int | None = 20_000,
        provider_switch_delay_ms: int = PROVIDER_SWITCH_DELAY_MS,
        stop_requested: Callable[[], bool] | None = None,
        pause_wait: Callable[[], None] | None = None,
        browser_background: bool = True,
        capture_validator: Callable[[Path], CaptureValidation] = validate_capture,
    ) -> None:
        self.profile_root = profile_root or Path.home() / ".archer-prosess" / "browser_profiles"
        self.channel = channel
        self.navigation_timeout_ms = navigation_timeout_ms
        self.analysis_timeout_ms = analysis_timeout_ms
        self.mtbp_cancer_type = mtbp_cancer_type.strip() or "Blood"
        self.clinvar_api_key = clinvar_api_key.strip()
        self.cosmic_email = cosmic_email.strip()
        self.cosmic_password = cosmic_password
        self.oncokb_email = oncokb_email.strip()
        self.oncokb_password = oncokb_password
        self.franklin_email = franklin_email.strip()
        self.franklin_password = franklin_password
        self.mtbp_email = mtbp_email.strip()
        self.mtbp_password = mtbp_password
        self.franklin_attempts = max(1, franklin_attempts)
        self.request_delay_ms = max(0, request_delay_ms)
        self.request_delay_max_ms = max(
            self.request_delay_ms,
            self.request_delay_ms
            if request_delay_max_ms is None
            else request_delay_max_ms,
        )
        self.provider_switch_delay_ms = max(0, int(provider_switch_delay_ms))
        self.stop_requested = stop_requested or (lambda: False)
        self.pause_wait = pause_wait or (lambda: None)
        self.browser_background = bool(browser_background)
        self.capture_validator = capture_validator
        self._cosmic_cache: dict[str, DatabaseEvidence] = {}
        self._mtbp_rejected_transcript_queries: set[str] = set()

    @staticmethod
    def dependency_available() -> bool:
        try:
            from archer_processor.services.edge_cdp import edge_cdp_available
        except ImportError:
            return False
        return edge_cdp_available()

    def profile_directory(self, database: str) -> Path:
        self._validate_database(database)
        directory = self.profile_root / database.lower().replace(" ", "-")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def login_url(self, database: str) -> str:
        self._validate_database(database)
        return LOGIN_URLS[database]

    def query_url(self, database: str, variant: VariantRecord) -> str:
        self._validate_database(database)
        if database == "COSMIC":
            cosmic_id = _cosmic_identifier(variant.cosmic_id)
            if not cosmic_id:
                return ""
            return (
                "https://cancer.sanger.ac.uk/cosmic/search?q="
                f"{quote(cosmic_id, safe='')}"
            )
        if database == "OncoKB":
            alteration = _protein_change(variant.hgvsp) or _cdna_change(variant.hgvsc)
            if not variant.symbol or not alteration:
                return ""
            return (
                "https://www.oncokb.org/gene/"
                f"{quote(variant.symbol, safe='')}/somatic/{quote(alteration, safe='')}"
            )
        if database == "ClinVar":
            query = variant.hgvsc or _review_query(variant)
            return (
                "https://www.ncbi.nlm.nih.gov/clinvar/?term="
                f"{quote(query, safe='')}"
            )
        if database == "Franklin":
            return FRANKLIN_HOME_URL if _franklin_search_query(variant) else ""
        return LOGIN_URLS[database]

    def open_login(self, database: str, *, maximum_minutes: int = 30) -> str:
        """Open login and release the profile as soon as authentication succeeds."""
        sync_browser, browser_error, _ = self._browser_api()
        profile = self.profile_directory(database)
        with sync_browser() as runtime:
            try:
                context = runtime.chromium.launch_persistent_context(
                    str(profile),
                    channel=self.channel,
                    headless=False,
                    accept_downloads=True,
                )
            except Exception as exc:
                raise BrowserAutomationUnavailable(
                    "Could not start Microsoft Edge for browser review. "
                    "Confirm that managed Edge is installed and its "
                    "RemoteDebuggingAllowed policy is enabled."
                ) from exc
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                self.login_url(database),
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            page.bring_to_front()
            if self._try_saved_login(database, page):
                context.close()
                return f"{database} signed in using Windows Credential Manager."
            deadline = datetime.now(timezone.utc).timestamp() + maximum_minutes * 60
            try:
                while context.pages and datetime.now(timezone.utc).timestamp() < deadline:
                    active_page = context.pages[0]
                    if self._session_authenticated(database, active_page):
                        return f"{database} sign-in confirmed; browser profile released."
                    active_page.wait_for_timeout(500)
            except browser_error:
                pass
            finally:
                try:
                    context.close()
                except browser_error:
                    pass
        return f"{database} sign-in window closed; session will be checked during lookup."

    def search_variants(
        self,
        variants: Iterable[VariantRecord],
        databases: Iterable[str],
        artifact_root: Path,
        *,
        progress: Callable[[str], None] | None = None,
        activity: Callable[[str, str], None] | None = None,
        completed_sources: set[tuple[str, str]] | None = None,
        checkpoint: Callable[[dict[str, list[DatabaseEvidence]]], None] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        variant_list = list(variants)
        requested = set(databases)
        database_list = [database for database in BROWSER_DATABASES if database in requested]
        results: dict[str, list[DatabaseEvidence]] = {
            self.variant_key(variant): [] for variant in variant_list
        }
        artifact_root.mkdir(parents=True, exist_ok=True)
        completed_sources = completed_sources or set()
        jobs = [
            (
                database,
                [
                    variant
                    for variant in variant_list
                    if (self.variant_key(variant), database) not in completed_sources
                ],
            )
            for database in database_list
        ]
        jobs = [(database, pending) for database, pending in jobs if pending]
        for database_index, (database, pending_variants) in enumerate(jobs):
            self._check_cancelled()
            if activity:
                activity(database, "Starting provider")

            def provider_progress(message: str, *, current=database) -> None:
                if progress:
                    progress(message)
                if activity:
                    activity(current, message)

            if progress:
                progress(
                    f"Browser review: starting {database} for "
                    f"{len(pending_variants)}/{len(variant_list)} pending variant(s)"
                )
            database_results = self._search_database(
                database,
                pending_variants,
                artifact_root / database.lower().replace(" ", "-"),
                progress=provider_progress,
            )
            for key, evidence in database_results.items():
                results[key].append(evidence)
            if checkpoint and database_results:
                checkpoint(
                    {
                        key: [evidence]
                        for key, evidence in database_results.items()
                    }
                )
            self._check_cancelled()
            if database_index < len(jobs) - 1:
                self._wait_between_databases(
                    database,
                    jobs[database_index + 1][0],
                    progress=provider_progress,
                )
        return results

    def _search_database(
        self,
        database: str,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        self._validate_database(database)
        if database == "MTBP":
            return self._search_mtbp(
                variants,
                artifact_directory,
                progress=progress,
            )
        if database == "Franklin":
            return self._search_franklin(
                variants,
                artifact_directory,
                progress=progress,
            )
        if database == "COSMIC":
            return self._search_cosmic(
                variants,
                artifact_directory,
                progress=progress,
            )
        if database == "ClinVar":
            return self._search_clinvar(
                variants,
                artifact_directory,
                progress=progress,
            )

        sync_browser, browser_error, browser_timeout = self._browser_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        results: dict[str, DatabaseEvidence] = {}
        with sync_browser() as runtime:
            try:
                context = runtime.chromium.launch_persistent_context(
                    str(self.profile_directory(database)),
                    channel=self.channel,
                    headless=False,
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 1000},
                    background=self.browser_background,
                )
            except Exception as exc:
                raise BrowserAutomationUnavailable(
                    "Could not start Microsoft Edge for browser review."
                ) from exc
            page = context.pages[0] if context.pages else context.new_page()
            try:
                if database == "OncoKB" and self.oncokb_email and self.oncokb_password:
                    page.goto(
                        self.login_url("OncoKB"),
                        wait_until="domcontentloaded",
                        timeout=self.navigation_timeout_ms,
                    )
                    if not self._try_saved_login("OncoKB", page):
                        return {
                            self.variant_key(variant): DatabaseEvidence(
                                "OncoKB",
                                "login_required",
                                "OncoKB sign-in failed. Check the saved email/password.",
                                accession=_review_query(variant),
                                url=page.url,
                            )
                            for variant in variants
                        }
                for index, variant in enumerate(variants, start=1):
                    self._check_cancelled()
                    key = self.variant_key(variant)
                    query_url = self.query_url(database, variant)
                    if not query_url:
                        results[key] = DatabaseEvidence(
                            database, "invalid_query", f"Cannot build a {database} web query."
                        )
                        continue
                    if progress:
                        progress(
                            f"{database} browser lookup {index}/{len(variants)}: "
                            f"{variant.symbol} {_protein_change(variant.hgvsp) or _cdna_change(variant.hgvsc)}"
                        )
                    try:
                        page.goto(
                            query_url,
                            wait_until="domcontentloaded",
                            timeout=self.navigation_timeout_ms,
                        )
                        if database == "OncoKB":
                            self._wait_for_oncokb_result(page)
                        else:
                            try:
                                page.wait_for_load_state("networkidle", timeout=12_000)
                            except browser_timeout:
                                page.wait_for_timeout(1_500)
                        results[key] = self._capture_result(
                            database, variant, page, artifact_directory
                        )
                    except Exception as exc:
                        results[key] = DatabaseEvidence(
                            database=database,
                            status="error",
                            summary=f"{database} browser lookup failed: {exc}",
                            accession=_review_query(variant),
                            url=query_url,
                        )
                    if index < len(variants):
                        self._wait_between_queries(
                            page, database, progress=progress
                        )
            finally:
                try:
                    context.close()
                except browser_error:
                    pass
        return results

    def _search_cosmic(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Capture the licensed COSMIC mutation page by Archer COSMICID."""
        sync_browser, browser_error, _ = self._browser_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        results: dict[str, DatabaseEvidence] = {}
        with sync_browser() as runtime:
            try:
                context = runtime.chromium.launch_persistent_context(
                    str(self.profile_directory("COSMIC")),
                    channel=self.channel,
                    headless=False,
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 1000},
                    background=self.browser_background,
                )
            except Exception as exc:
                raise BrowserAutomationUnavailable(
                    "Could not start COSMIC because its Edge profile is in use. "
                    "Close any COSMIC Edge window and retry."
                ) from exc
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    self.login_url("COSMIC"),
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                if not self._try_saved_login("COSMIC", page):
                    return {
                        self.variant_key(variant): DatabaseEvidence(
                            "COSMIC",
                            "login_required",
                            "COSMIC sign-in failed or is not configured. Save the "
                            "COSMIC email/password in Settings or use Sign In / Refresh.",
                            accession=variant.cosmic_id,
                            url=page.url,
                        )
                        for variant in variants
                    }

                for index, variant in enumerate(variants, start=1):
                    self._check_cancelled()
                    key = self.variant_key(variant)
                    query_url = self.query_url("COSMIC", variant)
                    if not query_url:
                        results[key] = DatabaseEvidence(
                            "COSMIC",
                            "invalid_query",
                            "No COSM/COSV identifier was present in the Archer COSMICID column.",
                        )
                        continue
                    cosmic_id = _cosmic_identifier(variant.cosmic_id)
                    if cosmic_id in self._cosmic_cache:
                        results[key] = self._cosmic_cache[cosmic_id]
                        if progress:
                            progress(f"COSMIC cache hit: {cosmic_id}")
                        continue
                    if progress:
                        progress(
                            f"COSMIC browser lookup {index}/{len(variants)}: "
                            f"{variant.cosmic_id}"
                        )
                    try:
                        page.goto(
                            query_url,
                            wait_until="domcontentloaded",
                            timeout=self.navigation_timeout_ms,
                        )
                        self._resolve_cosmic_mutation_page(page, variant)
                        if self._login_required("COSMIC", page.url):
                            results[key] = DatabaseEvidence(
                                "COSMIC",
                                "login_required",
                                "COSMIC session expired; use Sign In / Refresh and retry.",
                                accession=variant.cosmic_id,
                                url=page.url,
                            )
                        else:
                            self._wait_for_cosmic_result(page)
                            results[key] = self._capture_cosmic_result(
                                variant, page, artifact_directory
                            )
                            if results[key].status == "found":
                                self._cosmic_cache[cosmic_id] = results[key]
                    except Exception as exc:
                        results[key] = DatabaseEvidence(
                            "COSMIC",
                            "error",
                            f"COSMIC browser lookup failed: {exc}",
                            accession=variant.cosmic_id,
                            url=query_url,
                        )
                    if index < len(variants):
                        self._wait_between_queries(page, "COSMIC", progress=progress)
            finally:
                try:
                    context.close()
                except browser_error:
                    pass
        return results

    def _resolve_cosmic_mutation_page(
        self, page: Any, variant: VariantRecord
    ) -> None:
        """Resolve a legacy identifier and switch the canonical page to GRCh37."""
        cosmic_number = _cosmic_numeric_id(variant.cosmic_id)
        if not cosmic_number:
            raise ValueError("COSMIC identifier is missing its numeric component.")
        if "/cosmic/mutation/overview" not in page.url:
            links = page.locator("a[href*='/cosmic/mutation/overview']")
            attempts = max(1, self.navigation_timeout_ms // 500)
            for _ in range(attempts):
                candidates = [
                    href
                    for href in links.evaluate_all("nodes => nodes.map(node => node.href)")
                    if href and f"merge={cosmic_number}" in href
                ]
                candidates = list(dict.fromkeys(candidates))
                if len(candidates) == 1:
                    page.goto(
                        candidates[0],
                        wait_until="domcontentloaded",
                        timeout=self.navigation_timeout_ms,
                    )
                    break
                if len(candidates) > 1:
                    raise RuntimeError(
                        f"COSMIC returned multiple canonical pages for {variant.cosmic_id}."
                    )
                page.wait_for_timeout(500)
            else:
                raise RuntimeError(
                    f"COSMIC did not expose a canonical mutation link for {variant.cosmic_id}."
                )

        grch37_links = page.locator("a[href*='genome=37'][href*='id=']")
        candidates: list[str] = []
        for _ in range(max(1, self.navigation_timeout_ms // 500)):
            candidates = list(
                dict.fromkeys(
                    href
                    for href in grch37_links.evaluate_all(
                        "nodes => nodes.map(node => node.href)"
                    )
                    if href
                )
            )
            if candidates:
                break
            page.wait_for_timeout(500)
        if len(candidates) != 1:
            raise RuntimeError("COSMIC GRCh37 mutation link was not uniquely available.")
        page.goto(
            candidates[0],
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )

    def _search_clinvar(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Resolve ClinVar through E-utilities, then capture its summary card."""
        from archer_processor.services.database_search import DatabaseSearchService
        from archer_processor.services.settings import AppSettings

        sync_browser, browser_error, _ = self._browser_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        api_service = DatabaseSearchService(
            AppSettings(clinvar_api_key=self.clinvar_api_key)
        )
        results: dict[str, DatabaseEvidence] = {}
        with sync_browser() as runtime:
            try:
                context = runtime.chromium.launch_persistent_context(
                    str(self.profile_directory("ClinVar")),
                    channel=self.channel,
                    headless=False,
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 1000},
                    background=self.browser_background,
                )
            except Exception as exc:
                raise BrowserAutomationUnavailable(
                    "Could not start ClinVar because its Edge profile is in use. "
                    "Close any ClinVar Edge window and retry."
                ) from exc
            page = context.pages[0] if context.pages else context.new_page()
            try:
                for index, variant in enumerate(variants, start=1):
                    self._check_cancelled()
                    key = self.variant_key(variant)
                    if progress:
                        progress(
                            f"ClinVar browser lookup {index}/{len(variants)}: "
                            f"{variant.hgvsc or variant.display_name}"
                        )
                    api_evidence = api_service.search_variant(variant, ["ClinVar"])[0]
                    if api_evidence.status != "found" or not api_evidence.url:
                        results[key] = api_evidence
                        continue
                    if api_evidence.raw.get("assembly_verified") != "GRCh37":
                        api_evidence.status = "verification_required"
                        api_evidence.summary = (
                            "ClinVar result was not captured because its exact GRCh37 "
                            "identity was not verified."
                        )
                        results[key] = api_evidence
                        continue
                    try:
                        page.goto(
                            api_evidence.url,
                            wait_until="domcontentloaded",
                            timeout=self.navigation_timeout_ms,
                        )
                        results[key] = self._capture_clinvar_result(
                            variant,
                            api_evidence,
                            page,
                            artifact_directory,
                        )
                    except Exception as exc:
                        results[key] = DatabaseEvidence(
                            database="ClinVar",
                            status="error",
                            summary=f"ClinVar summary capture failed: {exc}",
                            accession=api_evidence.accession,
                            clinical_significance=api_evidence.clinical_significance,
                            url=api_evidence.url,
                            raw={**api_evidence.raw},
                        )
                    if index < len(variants):
                        self._wait_between_queries(page, "ClinVar", progress=progress)
            finally:
                try:
                    context.close()
                except browser_error:
                    pass
        return results

    def _capture_clinvar_result(
        self,
        variant: VariantRecord,
        api_evidence: DatabaseEvidence,
        page: Any,
        artifact_directory: Path,
    ) -> DatabaseEvidence:
        title = page.locator("main div.functions-container")
        summary = page.locator("#germline-somatic-info")
        title.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        summary.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        title_box = title.bounding_box()
        summary_box = summary.bounding_box()
        if title_box is None or summary_box is None:
            raise RuntimeError("ClinVar classification summary was not visible.")
        left = min(title_box["x"], summary_box["x"])
        top = min(title_box["y"], summary_box["y"])
        right = max(
            title_box["x"] + title_box["width"],
            summary_box["x"] + summary_box["width"],
        )
        bottom = max(
            title_box["y"] + title_box["height"],
            summary_box["y"] + summary_box["height"],
        )
        screenshot_path = self._screenshot_path(
            artifact_directory, "ClinVar", variant
        )
        page.screenshot(
            path=str(screenshot_path),
            clip={
                "x": max(0, left),
                "y": max(0, top),
                "width": right - left,
                "height": bottom - top,
            },
        )
        summary_text = " ".join(summary.inner_text().split())
        evidence = DatabaseEvidence(
            database="ClinVar",
            status="found",
            summary=api_evidence.summary,
            accession=api_evidence.accession,
            clinical_significance=api_evidence.clinical_significance,
            url=page.url,
            raw={
                **api_evidence.raw,
                "classification_summary": summary_text,
                "screenshot": str(screenshot_path),
                "screenshots": [
                    {
                        "label": "Classification summary",
                        "path": str(screenshot_path),
                        "url": page.url,
                    }
                ],
            },
        )
        self._write_audit(evidence, screenshot_path.with_suffix(".audit.json"))
        return evidence

    def _wait_for_cosmic_result(self, page: Any) -> None:
        overview = page.get_by_role("heading", name="Overview", exact=True)
        overview.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        samples = page.get_by_role("heading", name="Samples", exact=True)
        samples.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        rows = self._cosmic_section(page, "Samples").locator("tbody tr")
        attempts = max(1, self.navigation_timeout_ms // 500)
        for _ in range(attempts):
            if rows.count() > 0:
                return
            page.wait_for_timeout(500)
        raise TimeoutError("COSMIC sample table did not finish loading.")

    def _capture_cosmic_result(
        self,
        variant: VariantRecord,
        page: Any,
        artifact_directory: Path,
    ) -> DatabaseEvidence:
        body_text = page.locator("body").inner_text(timeout=self.navigation_timeout_ms)
        cosmic_id = _cosmic_identifier(variant.cosmic_id)
        cdna = _cdna_change(variant.hgvsc)
        protein = _protein_change(variant.hgvsp)
        variant_identity_matches = bool(
            variant.symbol
            and variant.symbol.casefold() in body_text.casefold()
            and any(
                value and value.casefold() in body_text.casefold()
                for value in (cdna, protein)
            )
        )
        if cosmic_id and cosmic_id not in body_text and not variant_identity_matches:
            return DatabaseEvidence(
                "COSMIC",
                "not_found",
                f"COSMIC did not resolve the requested identifier {cosmic_id}.",
                accession=cosmic_id,
                url=page.url,
            )

        base_path = self._screenshot_path(artifact_directory, "COSMIC", variant)
        overview = self._cosmic_section(page, "Overview")
        tissue = self._cosmic_section(page, "Tissue distribution")
        samples = self._cosmic_section(page, "Samples")
        search = samples.locator("input[type='search']")
        if search.count() != 1:
            raise RuntimeError("COSMIC Samples filter was not uniquely available.")
        search.fill("lymphoid")
        page.wait_for_timeout(1_200)
        rows = [text.strip() for text in samples.locator("tbody tr").all_inner_texts()]
        visible_lymphoid_rows = [text for text in rows if "lymphoid" in text.lower()]
        source_url = _cosmic_source_url(page.url, variant.cosmic_id)

        screenshot_specs = [
            ("COSMIC overview", overview, base_path),
            (
                "COSMIC tissue distribution",
                tissue,
                base_path.with_name(f"{base_path.stem}-tissue-distribution.png"),
            ),
            (
                "COSMIC samples filtered to lymphoid",
                samples,
                base_path.with_name(f"{base_path.stem}-samples-lymphoid.png"),
            ),
        ]
        screenshots: list[dict[str, str]] = []
        for label, section, screenshot_path in screenshot_specs:
            section.screenshot(path=str(screenshot_path))
            screenshots.append(
                {"label": label, "path": str(screenshot_path), "url": source_url}
            )

        summary = (
            f"COSMIC web evidence captured for {cosmic_id or variant.cosmic_id}: "
            "Overview, Tissue distribution, and Samples filtered to 'lymphoid'. "
            f"Visible matching sample rows={len(visible_lymphoid_rows)}."
        )
        evidence = DatabaseEvidence(
            database="COSMIC",
            status="found",
            summary=summary,
            accession=cosmic_id or variant.cosmic_id,
            url=source_url,
            raw={
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "screenshot": str(base_path),
                "screenshots": screenshots,
                "sample_filter": "lymphoid",
                "visible_lymphoid_sample_rows": visible_lymphoid_rows,
                "visible_text_preview": body_text[:12_000],
                "license_notice": (
                    "Confirm that the organisation's COSMIC licence permits use in "
                    "patient-care reporting before clinical deployment."
                ),
            },
        )
        self._write_audit(evidence, base_path.with_suffix(".audit.json"))
        return evidence

    @staticmethod
    def _cosmic_section(page: Any, heading: str):
        target_heading = page.get_by_role("heading", name=heading, exact=True)
        section = page.locator("section.draggable-section").filter(has=target_heading)
        if section.count() != 1:
            raise RuntimeError(f"COSMIC section was not uniquely available: {heading}")
        return section

    def _search_franklin(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Resolve HGVS through Franklin search, then extract only classification."""
        sync_browser, browser_error, browser_timeout = self._browser_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        results: dict[str, DatabaseEvidence] = {}
        context = None
        try:
            with sync_browser() as runtime:
                try:
                    context = runtime.chromium.launch_persistent_context(
                        str(self.profile_directory("Franklin")),
                        channel=self.channel,
                        headless=False,
                        accept_downloads=True,
                        viewport={"width": 1440, "height": 1000},
                        background=self.browser_background,
                    )
                except Exception as exc:
                    raise BrowserAutomationUnavailable(
                        "Could not start Franklin because its Edge profile is in use. "
                        "Close any Franklin Edge window and retry."
                    ) from exc
                page = context.pages[0] if context.pages else context.new_page()
                if self.franklin_email and self.franklin_password:
                    page.goto(
                        self.login_url("Franklin"),
                        wait_until="domcontentloaded",
                        timeout=self.navigation_timeout_ms,
                    )
                    if not self._try_saved_login("Franklin", page):
                        return {
                            self.variant_key(variant): DatabaseEvidence(
                                "Franklin",
                                "login_required",
                                "Franklin sign-in failed. Check the saved email/password.",
                                accession=_franklin_search_query(variant),
                                url=page.url,
                            )
                            for variant in variants
                        }

                for index, variant in enumerate(variants, start=1):
                    self._check_cancelled()
                    key = self.variant_key(variant)
                    if progress:
                        progress(
                            f"Franklin browser lookup {index}/{len(variants)}: "
                            f"{_franklin_search_query(variant)}"
                        )
                    results[key] = self._resolve_franklin_queries(
                        page,
                        variant,
                        artifact_directory,
                        progress=progress,
                    )
                    if index < len(variants):
                        self._wait_between_queries(
                            page, "Franklin", progress=progress
                        )
        finally:
            if context is not None:
                try:
                    context.close()
                except browser_error:
                    pass
        return results

    def _resolve_franklin_queries(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> DatabaseEvidence:
        started_at = time.monotonic()
        query_attempts: list[str] = []
        queries = _franklin_queries(variant)
        evidence = DatabaseEvidence(
            "Franklin",
            "invalid_query",
            "Cannot build a Franklin HGVS or genomic query.",
            url=FRANKLIN_HOME_URL,
        )
        for query_index, query in enumerate(queries, start=1):
            query_attempts.append(query)
            if progress and query_index > 1:
                progress(f"Franklin: retrying with GRCh37 genomic query {query}")
            evidence = self._search_franklin_query(
                page,
                variant,
                query,
                artifact_directory,
                progress=progress,
            )
            if evidence.status == "found":
                break
            if evidence.status not in {
                "identity_mismatch",
                "timeout",
                "not_found",
                "error",
            }:
                break
        evidence.raw["query_attempts"] = list(query_attempts)
        return persist_evidence_result(
            artifact_directory,
            "Franklin",
            variant,
            evidence,
            query_attempts=query_attempts,
            started_at=started_at,
        )

    def _search_franklin_query(
        self,
        page: Any,
        variant: VariantRecord,
        query: str,
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> DatabaseEvidence:
        _, _, browser_timeout = self._browser_api()
        last_text = ""
        last_error = ""
        last_status = "error"
        for attempt in range(1, self.franklin_attempts + 1):
            try:
                page.goto(
                    FRANKLIN_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                dismiss_known_overlays(page)
                search = page.locator(
                    "input[placeholder='Enter variant, gene or select an example above']"
                )
                search.wait_for(state="visible", timeout=self.navigation_timeout_ms)
                self._select_franklin_search_mode(page)
                search.fill(query)
                search.press("Enter")
                self._open_franklin_resolved_variant(page, variant)
                for _ in range(60):
                    self._check_cancelled()
                    last_text = page.locator("body").inner_text()
                    if (
                        self._franklin_result_ready(page, last_text)
                        or "Something went wrong" in last_text
                        or _franklin_quota_message(last_text)
                    ):
                        break
                    page.wait_for_timeout(1_000)
                if self._franklin_result_ready(page, last_text):
                    evidence = self._capture_result(
                        "Franklin", variant, page, artifact_directory
                    )
                    evidence.accession = query
                    if evidence.status != "found":
                        return evidence
                    try:
                        assessment = self._capture_franklin_assessment(
                            page, variant, artifact_directory
                        )
                        evidence.raw.setdefault("screenshots", []).extend(assessment)
                    except Exception as exc:
                        evidence.raw["screenshot_warning"] = (
                            "Franklin assessment-tools screenshot could not be captured: "
                            f"{exc}"
                        )
                        evidence.status = "partial_capture"
                        evidence.summary = (
                            f"{evidence.summary} Franklin screenshots require retry."
                        ).strip()
                    return evidence
                quota = _franklin_quota_message(last_text)
                if quota:
                    return DatabaseEvidence(
                        "Franklin",
                        "quota_exhausted",
                        quota,
                        accession=query,
                        url=page.url,
                    )
                last_error = "Franklin returned 'Something went wrong'."
            except browser_timeout:
                last_status = "timeout"
                last_error = "Franklin timed out while resolving the variant."
            except Exception as exc:
                last_status = "error"
                last_error = str(exc)
            if attempt < self.franklin_attempts:
                if progress:
                    progress(
                        f"Franklin: retrying {query} "
                        f"({attempt + 1}/{self.franklin_attempts})"
                    )
                page.wait_for_timeout(2_000 * attempt)
        return DatabaseEvidence(
            "Franklin",
            last_status,
            last_error or "Franklin did not return a classification.",
            accession=query,
            url=page.url,
            raw={"visible_text_preview": last_text[:12_000]},
        )

    @staticmethod
    def _franklin_result_ready(page: Any, body_text: str) -> bool:
        if "Suggested classification" in body_text:
            return True
        return (
            "/clinical-db/variant/snpTumor/" in page.url
            and "Somatic Clinical Evidence" in body_text
            and ("AMP Classification" in body_text or "Evidence for" in body_text)
        )

    def _open_franklin_resolved_variant(self, page: Any, variant: VariantRecord) -> None:
        for _ in range(40):
            if re.search(r"/clinical-db/variant/snp(?:Tumor)?/", page.url):
                return
            options = page.locator(".option")
            if options.count():
                option_texts = options.all_inner_texts()
                transcript = (variant.transcript or variant.hgvsc.split(":", 1)[0]).split(".", 1)[0]
                matches = [
                    index for index, text in enumerate(option_texts)
                    if transcript and transcript in text
                ]
                if len(matches) == 1:
                    selected = options.nth(matches[0])
                    try:
                        selected.wait_for(state="visible", timeout=2_000)
                        selected.click()
                        break
                    except Exception:
                        page.wait_for_timeout(500)
                        continue
                if len(option_texts) == 1:
                    try:
                        options.first.wait_for(state="visible", timeout=2_000)
                        options.first.click()
                        break
                    except Exception:
                        page.wait_for_timeout(500)
                        continue
                canonical = [
                    index for index, text in enumerate(option_texts)
                    if "Includes Canonical transcript" in text
                ]
                if len(canonical) == 1:
                    selected = options.nth(canonical[0])
                    try:
                        selected.wait_for(state="visible", timeout=2_000)
                        selected.click()
                        break
                    except Exception:
                        page.wait_for_timeout(500)
                        continue
                raise ValueError(
                    f"Franklin returned {len(option_texts)} ambiguous variants for "
                    f"{_franklin_search_query(variant)}."
                )
            page.wait_for_timeout(500)
        page.wait_for_url(
            re.compile(
                r"https://franklin\.genoox\.com/clinical-db/variant/"
                r"snp(?:Tumor)?/.+"
            ),
            timeout=self.navigation_timeout_ms,
        )

    def _select_franklin_search_mode(self, page: Any) -> None:
        """Choose the explicitly requested GRCh37/hg19 somatic search mode."""
        comboboxes = page.get_by_role("combobox")
        if comboboxes.count() < 2:
            raise RuntimeError("Franklin reference/type selectors were not available.")
        for combobox, option_name in (
            (comboboxes.nth(0), "hg19"),
            (comboboxes.nth(1), "Somatic"),
        ):
            current_text = (
                combobox.inner_text().strip().lower()
                if hasattr(combobox, "inner_text")
                else ""
            )
            if option_name.lower() in current_text:
                continue
            click_physical = getattr(combobox, "click_physical", combobox.click)
            click_physical()
            option = page.get_by_role("option", name=option_name, exact=True)
            option.wait_for(state="visible", timeout=self.navigation_timeout_ms)
            if option.count() != 1:
                raise RuntimeError(
                    f"Franklin search option was not uniquely available: {option_name}"
                )
            option_click = getattr(option, "click_physical", option.click)
            option_click()
            page.wait_for_timeout(150)

    def _search_mtbp(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Run independent MTBP reports so each screenshot belongs to one variant."""
        results: dict[str, DatabaseEvidence] = {}
        for index, variant in enumerate(variants, start=1):
            self._check_cancelled()
            if progress:
                progress(
                    f"MTBP variant {index}/{len(variants)}: "
                    f"{variant.symbol} {_protein_change(variant.hgvsp) or _cdna_change(variant.hgvsc)}"
                )
            current = self._search_mtbp_batch(
                [variant], artifact_directory, progress=progress
            )
            for evidence in current.values():
                evidence.url = ""
                for record in evidence.raw.get("screenshots", []):
                    if isinstance(record, dict):
                        record["url"] = ""
            results.update(current)
            if index < len(variants):
                delay_ms = self._next_request_delay_ms()
                if delay_ms > 0:
                    if progress:
                        progress(
                            f"MTBP: safety buffer {delay_ms / 1_000:.1f}s before next variant"
                        )
                    self._interruptible_sleep(delay_ms / 1_000)
        return results

    def _search_mtbp_batch(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Submit one pseudonymous MTBP analysis and parse its report."""
        results: dict[str, DatabaseEvidence] = {}
        query_pairs: list[tuple[VariantRecord, str]] = []
        initial_query_attempts: dict[str, list[str]] = {}
        learned_fallback_keys: set[str] = set()
        for variant in variants:
            query, attempts, learned_fallback = self._mtbp_initial_query(variant)
            if query:
                query_pairs.append((variant, query))
                initial_query_attempts[self.variant_key(variant)] = attempts
                if learned_fallback:
                    learned_fallback_keys.add(self.variant_key(variant))
            else:
                results[self.variant_key(variant)] = DatabaseEvidence(
                    "MTBP",
                    "invalid_query",
                    "Cannot build an MTBP protein, cDNA, or GRCh37 genomic query.",
                    accession=_review_query(variant),
                    url=self.login_url("MTBP"),
                )
        if not query_pairs:
            return results

        sync_browser, browser_error, browser_timeout = self._browser_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        submitted_queries = list(dict.fromkeys(query for _, query in query_pairs))
        batch_digest = hashlib.sha256("\n".join(submitted_queries).encode("utf-8")).hexdigest()[:10]
        analysis_id = (
            "ARCHER-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
            + batch_digest
        )
        context = None
        try:
            with sync_browser() as runtime:
                try:
                    context = runtime.chromium.launch_persistent_context(
                        str(self.profile_directory("MTBP")),
                        channel=self.channel,
                        headless=False,
                        accept_downloads=True,
                        viewport={"width": 1440, "height": 1000},
                        background=self.browser_background,
                    )
                except Exception as exc:
                    raise BrowserAutomationUnavailable(
                        "Could not start MTBP because its Edge profile is in use. "
                        "Close any MTBP Edge window and retry."
                    ) from exc
                page = context.pages[0] if context.pages else context.new_page()
                if progress:
                    progress(f"MTBP: submitting {len(submitted_queries)} pseudonymous variants")
                    if learned_fallback_keys:
                        progress(
                            "MTBP: using previously validated GRCh37 fallback for "
                            f"{len(learned_fallback_keys)} variant(s)"
                        )
                self._goto_with_retries(page, self.login_url("MTBP"))
                if not self._session_authenticated("MTBP", page):
                    self._try_saved_login("MTBP", page)
                if not self._session_authenticated("MTBP", page):
                    return {
                        **results,
                        **{
                            self.variant_key(variant): DatabaseEvidence(
                                "MTBP",
                                "login_required",
                                "MTBP sign-in is required. Save the MTBP email/password "
                                "in Settings or use the Browser Sign-in button.",
                                accession=query,
                                url=page.url,
                            )
                            for variant, query in query_pairs
                        },
                    }

                preflight_cleanup = self._cleanup_stale_mtbp_reports(
                    page,
                    progress=progress,
                )
                self._goto_with_retries(page, self.login_url("MTBP"))
                active_pairs = list(query_pairs)
                query_attempts = {
                    self.variant_key(variant): list(
                        initial_query_attempts[self.variant_key(variant)]
                    )
                    for variant, query in query_pairs
                }
                fallback_keys = set(learned_fallback_keys)
                validation_round = 0
                while active_pairs:
                    validation_round += 1
                    submitted_queries = list(
                        dict.fromkeys(query for _, query in active_pairs)
                    )
                    run_analysis_id = (
                        analysis_id
                        if validation_round == 1
                        else f"{analysis_id}-R{validation_round}"
                    )
                    self._fill_mtbp_form(
                        page,
                        run_analysis_id,
                        submitted_queries,
                    )
                    page.locator("#run-analysis").click()
                    validation_text = self._wait_for_mtbp_acceptance(page)
                    if not validation_text:
                        analysis_id = run_analysis_id
                        break
                    unmapped = _mtbp_unmapped_queries(validation_text)
                    rejected_pairs = [
                        pair for pair in active_pairs
                        if _mtbp_query_rejected(pair[1], unmapped)
                    ]
                    if not rejected_pairs:
                        raise ValueError(
                            "MTBP rejected the batch, but the unmappable entries "
                            "could not be matched safely to submitted variants."
                        )
                    replacement_by_key: dict[str, tuple[VariantRecord, str]] = {}
                    finally_rejected: list[tuple[VariantRecord, str]] = []
                    for variant, query in rejected_pairs:
                        key = self.variant_key(variant)
                        fallback = _mtbp_genomic_query(variant)
                        if fallback and fallback not in query_attempts[key]:
                            self._mtbp_rejected_transcript_queries.add(query)
                            query_attempts[key].append(fallback)
                            replacement_by_key[key] = (variant, fallback)
                            fallback_keys.add(key)
                        else:
                            finally_rejected.append((variant, query))
                    for variant, query in finally_rejected:
                        results[self.variant_key(variant)] = DatabaseEvidence(
                            "MTBP",
                            "invalid_query",
                            "MTBP could not map this variant after transcript and "
                            "GRCh37 genomic attempts; it was removed so the remaining "
                            "batch could continue.",
                            accession=query,
                            url=page.url,
                            raw={
                                "submitted_query": query,
                                "query_attempts": query_attempts[self.variant_key(variant)],
                                "validation_error": validation_text[:4_000],
                            },
                        )
                    rejected_keys = {
                        self.variant_key(variant) for variant, _ in finally_rejected
                    }
                    active_pairs = [
                        replacement_by_key.get(self.variant_key(pair[0]), pair)
                        for pair in active_pairs
                        if self.variant_key(pair[0]) not in rejected_keys
                    ]
                    if progress:
                        if replacement_by_key:
                            progress(
                                "MTBP: transcript form was not accepted for "
                                f"{len(replacement_by_key)} variant(s); retrying safely "
                                "with GRCh37 genomic HGVS"
                            )
                        if finally_rejected:
                            progress(
                                f"MTBP: removed {len(finally_rejected)} unmappable variant(s); "
                                f"continuing with {len(active_pairs)}"
                            )
                    if active_pairs:
                        self._goto_with_retries(page, self.login_url("MTBP"))
                if not active_pairs:
                    return results
                if "/queue/" in page.url:
                    if progress:
                        progress(
                            "MTBP: analysis queued; waiting up to "
                            f"{self.analysis_timeout_ms // 60_000} minutes for the report"
                        )
                    self._wait_for_mtbp_report(
                        page,
                        analysis_id,
                        browser_timeout,
                        progress=progress,
                    )
                if progress:
                    progress("MTBP: report ready; validating returned variants")
                body_text = page.locator("body").inner_text(timeout=self.navigation_timeout_ms)
                version_tooltip = page.locator("[data-tooltip-html*='VEP:']")
                if version_tooltip.count():
                    version_html = version_tooltip.first.get_attribute("data-tooltip-html") or ""
                    body_text += "\n" + re.sub(r"<br\s*/?>", "\n", version_html, flags=re.IGNORECASE)
                report_rows = self._extract_mtbp_rows(page)
                parsed = parse_mtbp_report(
                    body_text,
                    report_rows,
                    [variant for variant, _ in active_pairs],
                    page.url,
                    cancer_type=self.mtbp_cancer_type,
                )
                if progress and fallback_keys:
                    accepted_fallbacks = sum(
                        parsed[self.variant_key(variant)].status == "found"
                        for variant, _ in active_pairs
                        if self.variant_key(variant) in fallback_keys
                    )
                    progress(
                        "MTBP: GRCh37 fallback accepted for "
                        f"{accepted_fallbacks}/{len(fallback_keys)} variant(s)"
                    )
                captured_at = datetime.now(timezone.utc).isoformat()
                audit_records: list[tuple[Path, DatabaseEvidence]] = []
                for variant, query in active_pairs:
                    key = self.variant_key(variant)
                    evidence = parsed[key]
                    evidence.accession = query
                    screenshot_path = None
                    if evidence.status == "found":
                        try:
                            screenshot_path = self._capture_mtbp_variant_screenshot(
                                page, variant, artifact_directory
                            )
                        except IncompleteCaptureError as exc:
                            evidence.status = "partial_capture"
                            evidence.summary = (
                                f"{evidence.summary} MTBP screenshot requires retry."
                            ).strip()
                            evidence.raw["capture_validation"] = {
                                "valid": False,
                                "reason": exc.validation.reason,
                            }
                    screenshot_records = (
                        [
                            {
                                "label": "Alteration-centric functional evidence",
                                "path": str(screenshot_path),
                                "url": "",
                            }
                        ]
                        if screenshot_path is not None
                        else []
                    )
                    evidence.raw.update(
                        {
                            "analysis_id": analysis_id,
                            "submitted_query": query,
                            "query_attempts": query_attempts[key],
                            "captured_at": captured_at,
                            "remote_report_preflight_cleanup": preflight_cleanup,
                            "screenshot": str(screenshot_path or ""),
                            "screenshots": screenshot_records,
                            "visible_text_preview": body_text[:12_000],
                        }
                    )
                    audit_path = self._screenshot_path(
                        artifact_directory, "MTBP", variant
                    ).with_suffix(".audit.json")
                    audit_records.append((audit_path, evidence))
                    results[key] = evidence
                try:
                    remote_cleanup = self._cleanup_stale_mtbp_reports(
                        page,
                        progress=progress,
                    )
                except Exception as exc:
                    remote_cleanup = {
                        "status": "failed",
                        "message": f"Post-capture MTBP cleanup failed: {exc}",
                    }
                if progress:
                    if remote_cleanup["status"] == "retained":
                        progress(
                            "MTBP: remote report kept; cleanup threshold not reached "
                            f"({analysis_id})"
                        )
                    else:
                        progress(
                            "MTBP: remote report housekeeping "
                            f"{remote_cleanup['status']} ({analysis_id})"
                        )
                    found_count = sum(
                        item.status == "found"
                        for item in results.values()
                        if item.database == "MTBP"
                    )
                    progress(
                        f"MTBP: completed with {found_count}/{len(query_pairs)} "
                        "variant(s) matched"
                    )
                for audit_path, evidence in audit_records:
                    evidence.raw["remote_report_cleanup"] = remote_cleanup
                    audit_path.write_text(
                        json.dumps(asdict(evidence), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        except MtbpReportTimeout:
            current_url = context.pages[0].url if context and context.pages else self.login_url("MTBP")
            for variant, query in query_pairs:
                key = self.variant_key(variant)
                if key in results:
                    continue
                results[key] = DatabaseEvidence(
                    "MTBP",
                    "timeout",
                    "MTBP did not produce a report before the configured "
                    f"{self.analysis_timeout_ms // 60_000}-minute timeout.",
                    accession=query,
                    url=current_url,
                )
        except (browser_timeout, TimeoutError) as exc:
            current_url = context.pages[0].url if context and context.pages else self.login_url("MTBP")
            for variant, query in query_pairs:
                key = self.variant_key(variant)
                if key in results:
                    continue
                results[key] = DatabaseEvidence(
                    "MTBP",
                    "error",
                    "An MTBP page operation timed out before report polling completed. "
                    f"The remote report may still be available in Reports List: {exc}",
                    accession=query,
                    url=current_url,
                )
        except Exception as exc:
            for variant, query in query_pairs:
                key = self.variant_key(variant)
                if key in results:
                    continue
                results[key] = DatabaseEvidence(
                    "MTBP",
                    "error",
                    f"MTBP browser lookup failed: {exc}",
                    accession=query,
                    url=self.login_url("MTBP"),
                )
        finally:
            if context is not None:
                try:
                    context.close()
                except browser_error:
                    pass
        return results

    def _mtbp_initial_query(
        self, variant: VariantRecord
    ) -> tuple[str, list[str], bool]:
        primary = _mtbp_variant_query(variant)
        if not primary:
            return "", [], False
        if primary not in self._mtbp_rejected_transcript_queries:
            return primary, [primary], False
        fallback = _mtbp_genomic_query(variant)
        if not fallback or fallback == primary:
            return primary, [primary], False
        return fallback, [primary, fallback], True

    def _cleanup_stale_mtbp_reports(
        self,
        page: Any,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        """Batch-delete app reports at the threshold or when capacity is exhausted."""
        self._goto_with_retries(page, MTBP_REPORTS_URL)
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        remaining = page.locator("button.delete-patient").count()
        generated = page.locator(
            "button.delete-patient[data-patient-name^='ARCHER-']"
        )
        generated_count = generated.count()
        threshold_reached = generated_count >= MTBP_ARCHER_CLEANUP_THRESHOLD
        capacity_reached = remaining >= MTBP_REPORT_LIMIT
        if not threshold_reached and not capacity_reached:
            return {
                "status": "retained",
                "trigger": "none",
                "deleted_stale_reports": [],
                "failed_deletions": [],
                "remaining_reports": remaining,
                "remaining_archer_reports": generated_count,
            }

        trigger = "archer_threshold" if threshold_reached else "report_capacity"
        for _ in range(MTBP_REPORT_LIMIT + 1):
            generated = page.locator(
                "button.delete-patient[data-patient-name^='ARCHER-']"
            )
            generated_count = generated.count()
            if generated_count == 0:
                break
            analysis_id = (
                generated.nth(0).get_attribute("data-patient-name")
                or ""
            )
            if not analysis_id.startswith("ARCHER-"):
                failed.append(
                    {"analysis_id": analysis_id, "reason": "unsafe identifier"}
                )
                break
            outcome = self._delete_mtbp_report(page, analysis_id)
            if outcome["status"] == "deleted":
                deleted.append(analysis_id)
                if progress:
                    progress(f"MTBP: deleted stale application report {analysis_id}")
            else:
                failed.append(
                    {
                        "analysis_id": analysis_id,
                        "reason": outcome.get("message", outcome["status"]),
                    }
                )
                break
            remaining = page.locator("button.delete-patient").count()

        generated_remaining = page.locator(
            "button.delete-patient[data-patient-name^='ARCHER-']"
        ).count()
        if generated_remaining:
            raise RuntimeError(
                "MTBP batch cleanup did not remove every ARCHER report; "
                f"{generated_remaining} application report(s) remain."
            )
        if remaining >= MTBP_REPORT_LIMIT:
            raise RuntimeError(
                f"MTBP has {remaining} reports and allows only {MTBP_REPORT_LIMIT}. "
                "No additional application-created ARCHER report could be removed safely; "
                "delete an older report manually in the MTBP Reports List."
            )
        return {
            "status": "deleted_batch",
            "trigger": trigger,
            "deleted_stale_reports": deleted,
            "failed_deletions": failed,
            "remaining_reports": remaining,
            "remaining_archer_reports": generated_remaining,
        }

    def _delete_mtbp_report(self, page: Any, analysis_id: str) -> dict[str, str]:
        """Delete one exact app-generated report and never touch manual report IDs."""
        if not analysis_id.startswith("ARCHER-"):
            return {
                "status": "skipped",
                "message": "Only ARCHER-prefixed reports may be deleted automatically.",
            }
        try:
            if not page.url.startswith(MTBP_REPORTS_URL):
                self._goto_with_retries(page, MTBP_REPORTS_URL)
            report_link = page.get_by_role("link", name=analysis_id, exact=True)
            if report_link.count() != 1:
                return {
                    "status": "not_found",
                    "message": "The exact generated report was not present in the report list.",
                }
            row = report_link.locator("xpath=ancestor::tr[1]")
            delete_button = row.locator("button.delete-patient")
            if delete_button.count() != 1:
                return {
                    "status": "failed",
                    "message": "The exact generated report did not have one delete control.",
                }
            if delete_button.get_attribute("data-patient-name") != analysis_id:
                return {
                    "status": "failed",
                    "message": "The delete control did not match the generated analysis ID.",
                }

            def accept_confirmation(dialog: Any) -> None:
                if dialog.type == "confirm":
                    dialog.accept()
                else:
                    dialog.dismiss()

            page.once("dialog", accept_confirmation)
            delete_button.click()
            try:
                report_link.wait_for(
                    state="detached",
                    timeout=self.navigation_timeout_ms,
                )
            except Exception:
                self._goto_with_retries(page, MTBP_REPORTS_URL)
            if page.get_by_role("link", name=analysis_id, exact=True).count() == 0:
                return {
                    "status": "deleted",
                    "message": "The generated MTBP report was removed after local capture.",
                }
            return {
                "status": "failed",
                "message": "MTBP still listed the generated report after deletion.",
            }
        except Exception as exc:
            return {
                "status": "failed",
                "message": f"Could not delete the generated MTBP report: {exc}",
            }

    def _fill_mtbp_form(
        self,
        page: Any,
        analysis_id: str,
        submitted_queries: list[str],
    ) -> None:
        page.locator("#analysis-id").fill(analysis_id)
        cancer_item = page.get_by_role(
            "treeitem", name=self.mtbp_cancer_type, exact=True
        )
        if cancer_item.count() != 1:
            search = page.locator("#cancer-search")
            search.fill(self.mtbp_cancer_type)
            page.wait_for_timeout(350)
            cancer_item = page.get_by_role(
                "treeitem", name=self.mtbp_cancer_type, exact=True
            )
        if cancer_item.count() != 1:
            raise ValueError(
                f"MTBP cancer type was not found: {self.mtbp_cancer_type!r}"
            )
        cancer_item.click()
        page.locator("#variant-input").fill("\n".join(submitted_queries))

    def _wait_for_mtbp_acceptance(self, page: Any) -> str:
        for _ in range(max(1, self.navigation_timeout_ms // 500)):
            if re.search(r"https://mtbp\.org/(?:queue/\d+/?|patients/.+/report/\d+/?)", page.url):
                return ""
            body_text = page.locator("body").inner_text()
            if "cannot be mapped to genomic coordinates" in body_text:
                return body_text
            page.wait_for_timeout(500)
        raise TimeoutError("MTBP did not accept the submitted batch before the navigation timeout.")

    def _wait_for_mtbp_report(
        self,
        page: Any,
        analysis_id: str,
        browser_timeout: type[Exception],
        *,
        progress: Callable[[str], None] | None,
    ) -> None:
        """Poll both the queue and Reports List, recovering from transient navigation stalls."""
        report_pattern = re.compile(
            r"https://mtbp\.org/patients/.+/report/\d+/?"
        )
        queue_url = page.url
        deadline = time.monotonic() + self.analysis_timeout_ms / 1_000
        next_update = time.monotonic() + 60
        while time.monotonic() < deadline:
            self._check_cancelled()
            if report_pattern.fullmatch(page.url):
                return
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
            try:
                page.wait_for_url(
                    report_pattern,
                    timeout=min(15_000, remaining_ms),
                )
                if report_pattern.fullmatch(page.url):
                    return
            except browser_timeout:
                pass

            # MTBP occasionally leaves the queue page unchanged after the report
            # is already listed. Checking the exact pseudonymous ID recovers that
            # completed report without submitting a duplicate analysis.
            try:
                self._goto_with_retries(page, MTBP_REPORTS_URL, attempts=2)
                report_link = page.get_by_role(
                    "link", name=analysis_id, exact=True
                )
                if report_link.count() == 1:
                    report_link.click()
                    page.wait_for_url(
                        report_pattern,
                        timeout=self.navigation_timeout_ms,
                    )
                    return
            except browser_timeout:
                pass
            except Exception:
                # A transient reports-list failure should not abandon the active
                # queue; retry it until the configured analysis deadline.
                pass

            if time.monotonic() >= next_update:
                if progress:
                    minutes_left = max(
                        1, int((deadline - time.monotonic() + 59) // 60)
                    )
                    progress(
                        "MTBP: still processing; checked queue and Reports List "
                        f"({minutes_left} minute(s) remaining)"
                    )
                next_update = time.monotonic() + 60
            try:
                self._goto_with_retries(page, queue_url, attempts=2)
            except Exception:
                page.wait_for_timeout(1_000)

        # Perform one final reports-list recovery check at the deadline.
        try:
            self._goto_with_retries(page, MTBP_REPORTS_URL, attempts=2)
            report_link = page.get_by_role("link", name=analysis_id, exact=True)
            if report_link.count() == 1:
                report_link.click()
                page.wait_for_url(
                    report_pattern,
                    timeout=self.navigation_timeout_ms,
                )
                return
        except Exception:
            pass
        raise MtbpReportTimeout(
            f"MTBP report {analysis_id} was not published before the deadline."
        )

    def _goto_with_retries(
        self,
        page: Any,
        url: str,
        *,
        attempts: int = 3,
    ) -> None:
        """Retry idempotent MTBP navigation after short network/browser stalls."""
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    page.wait_for_timeout(1_000 * attempt)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _extract_mtbp_rows(page: Any) -> list[dict[str, Any]]:
        """Read only the visible alteration-centric report tables."""
        return page.locator("table").evaluate_all(
            """
            tables => tables
              .filter(table => !!(table.offsetWidth || table.offsetHeight || table.getClientRects().length))
              .flatMap(table => {
                const rows = [...table.rows];
                if (!rows.length) return [];
                const headers = [...rows[0].cells].map(cell => cell.innerText.trim());
                if (headers[0] !== 'Gene' || headers[2] !== 'Alteration' ||
                    !headers[4]?.startsWith('Reported biomarker')) return [];
                const accordion = table.closest('.accordion-item');
                const section = accordion?.firstElementChild?.innerText?.trim() || '';
                return rows.slice(1).map(row => {
                  const cells = [...row.cells];
                  const geneText = cells[0]?.innerText.trim() || '';
                  return {
                    section,
                    gene: (geneText.match(/[A-Za-z0-9-]+/) || [''])[0],
                    gene_text: geneText,
                    gene_info: cells[1]?.innerText.trim() || '',
                    alteration: cells[2]?.innerText.trim() || '',
                    identity_text: cells[2]?.textContent?.trim() || '',
                    functional_evidence: cells[3]?.innerText.trim() || '',
                    biomarkers: cells[4]?.innerText.trim() || '',
                    source_links: [...row.querySelectorAll('a[href]')].map(a => a.href)
                  };
                });
              })
            """
        )

    def _capture_result(
        self,
        database: str,
        variant: VariantRecord,
        page: Any,
        artifact_directory: Path,
    ) -> DatabaseEvidence:
        dismiss_known_overlays(page)
        if database == "OncoKB":
            self._reject_oncokb_cookies(page)
        current_url = page.url
        if self._login_required(database, current_url):
            return DatabaseEvidence(
                database=database,
                status="login_required",
                summary=f"Sign in to {database} using the Browser Sign-in button, then retry.",
                accession=_review_query(variant),
                url=current_url,
            )
        if database == "Franklin":
            screenshots = self._capture_franklin_computed_pages(
                page, variant, artifact_directory
            )
            screenshot_path = Path(screenshots[0]["path"])
            # Parse the ACMG view after both Computed Classification subtabs have
            # been captured and the ACMG subtab has been restored.
            body_text = page.locator("body").inner_text(
                timeout=self.navigation_timeout_ms
            )
        else:
            body_text = page.locator("body").inner_text(
                timeout=self.navigation_timeout_ms
            )
            screenshot_path = self._screenshot_path(
                artifact_directory, database, variant
            )
            page.screenshot(path=str(screenshot_path), full_page=False)
            screenshots = [
                {
                    "label": self._screenshot_label(database),
                    "path": str(screenshot_path),
                    "url": current_url,
                }
            ]
        if database == "OncoKB":
            evidence = parse_oncokb_page(body_text, variant, current_url)
        else:
            evidence = parse_franklin_page(body_text, variant, current_url)
        evidence.raw.update(
            {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "screenshot": str(screenshot_path),
                "screenshots": screenshots,
                "visible_text_preview": body_text[:12_000],
            }
        )
        audit_path = screenshot_path.with_suffix(".audit.json")
        self._write_audit(evidence, audit_path)
        return evidence

    def _reject_oncokb_cookies(self, page: Any) -> None:
        """Persist the privacy-preserving choice and keep overlays out of captures."""
        reject = page.get_by_role("button", name="Reject all", exact=True)
        if reject.count() == 1 and reject.is_visible():
            reject.click()
            page.wait_for_timeout(350)
        page.locator(
            "#onetrust-banner-sdk, #onetrust-consent-sdk, "
            ".onetrust-pc-dark-filter, [class*='cookie-consent']"
        ).evaluate_all(
            "nodes => nodes.forEach(node => node.remove())"
        )

    def _capture_franklin_computed_pages(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
    ) -> list[dict[str, str]]:
        """Capture the ACMG and oncogenic Computed Classification subtabs."""
        try:
            computed_tab = page.get_by_role(
                "tab", name="Computed Classification", exact=True
            )
        except (AttributeError, TypeError):
            return self._capture_franklin_classification(
                page, variant, artifact_directory
            )
        if computed_tab.count() != 1:
            return self._capture_franklin_classification(
                page, variant, artifact_directory
            )

        computed_tab.click()
        page.get_by_text("Suggested classification", exact=True).first.wait_for(
            state="visible", timeout=self.navigation_timeout_ms
        )
        page.wait_for_timeout(500)
        self._close_franklin_side_panel(page)
        self._activate_franklin_classification_subtab(page, "ACMG Classification")
        screenshots = self._capture_franklin_classification(
            page, variant, artifact_directory
        )
        self._activate_franklin_classification_subtab(
            page, "Oncogenic Classification"
        )
        screenshots.extend(
            self._capture_franklin_oncogenic_classification(
                page, variant, artifact_directory
            )
        )
        # Keep ACMG visible so the existing parser reads the germline-style
        # suggested classification rather than the oncogenic label.
        self._activate_franklin_classification_subtab(page, "ACMG Classification")
        return screenshots

    def _close_franklin_side_panel(self, page: Any) -> None:
        try:
            close_button = page.locator("gnx-mini-app-header img.close-btn")
            if close_button.count() == 1 and close_button.is_visible():
                close_button.click()
                page.wait_for_timeout(150)
        except Exception:
            pass
        page.evaluate("window.scrollTo(0, window.scrollY)")

    def _activate_franklin_classification_subtab(
        self, page: Any, label: str
    ) -> None:
        tab = page.get_by_text(label, exact=True)
        if tab.count() != 1:
            raise RuntimeError(f"Franklin subtab was not uniquely available: {label}")
        # Franklin attaches the subtab handler to the parent span. A physical
        # click on the child text becomes unreliable after the internal ACMG
        # evidence scroller has moved, while clicking the owning tab container
        # consistently switches the Angular view.
        tab.evaluate("el => { el.parentElement.click(); return true; }")
        target_selector = (
            "gnx-result-page"
            if label == "ACMG Classification"
            else "gnx-oncogenic-classification-app"
        )
        page.locator(target_selector).wait_for(
            state="visible", timeout=self.navigation_timeout_ms
        )
        page.evaluate("window.scrollTo(0, window.scrollY)")
        page.wait_for_timeout(250)

    def _wait_for_nonempty_category_titles(
        self,
        page: Any,
        categories: Any,
        *,
        required_title: str | None = None,
    ) -> list[str]:
        for _ in range(max(1, self.navigation_timeout_ms // 500)):
            self._check_cancelled()
            texts = categories.all_inner_texts()
            titles = [_first_nonempty_line(text) for text in texts]
            if (
                titles
                and all(titles)
                and (required_title is None or required_title in titles)
            ):
                return titles
            page.wait_for_timeout(500)
        raise IncompleteCaptureError(
            CaptureValidation(False, "semantic_content_missing", 0, 0, 0.0)
        )

    def _capture_with_incident_retry(
        self,
        page: Any,
        path: Path,
        capture: Callable[[], None],
    ) -> CaptureValidation:
        capture()
        validation = self.capture_validator(path)
        if validation.valid:
            return validation
        self._interruptible_page_wait(page, 5_000)
        capture()
        validation = self.capture_validator(path)
        if not validation.valid:
            raise IncompleteCaptureError(validation)
        return validation

    def _capture_franklin_classification(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
    ) -> list[dict[str, str]]:
        """Capture a classification-only overview and each ACMG evidence box."""
        original_path = self._screenshot_path(
            artifact_directory, "Franklin", variant
        )
        base_path = original_path.with_name(
            f"{original_path.stem}-computed{original_path.suffix}"
        )
        panel = page.locator("gnx-result-page")
        if panel.count() != 1:
            page.screenshot(path=str(base_path), full_page=True)
            return [
                {
                    "label": "Full ACMG classification page",
                    "path": str(base_path),
                    "url": page.url,
                }
            ]

        panel.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        panel.evaluate("el => { el.scrollTop = 0; }")
        categories = panel.locator("gnx-result-category")
        category_titles = self._wait_for_nonempty_category_titles(
            page,
            categories,
            required_title="De Novo Data",
        )
        self._capture_with_incident_retry(
            page,
            base_path,
            lambda: self._capture_franklin_classification_overview(
                page, panel, categories.nth(0), base_path,
                include_gene_header=True,  # Include gene header for first ACMG screenshot
            ),
        )
        screenshots = [
            {
                "label": "ACMG classification overview",
                "path": str(base_path),
                "url": page.url,
            }
        ]
        de_novo_indexes = [
            index for index, title in enumerate(category_titles)
            if title == "De Novo Data"
        ]
        if len(de_novo_indexes) != 1:
            raise RuntimeError("Franklin ACMG De Novo evidence boundary was unavailable.")
        for index in range(de_novo_indexes[0] + 1):
            category = categories.nth(index)
            title = category_titles[index]
            screenshot_path = base_path.with_name(
                f"{base_path.stem}-evidence-{index + 1:02d}{base_path.suffix}"
            )
            category.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            self._capture_with_incident_retry(
                page,
                screenshot_path,
                lambda category=category, path=screenshot_path: (
                    self._capture_franklin_evidence_box(page, category, path)
                ),
            )
            screenshots.append(
                {
                    "label": f"ACMG evidence: {title}",
                    "path": str(screenshot_path),
                    "url": page.url,
                }
            )
        panel.evaluate("el => { el.scrollTop = 0; }")
        return screenshots

    def _capture_franklin_oncogenic_classification(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
    ) -> list[dict[str, str]]:
        """Capture the oncogenic overview and each named evidence box."""
        original_path = self._screenshot_path(
            artifact_directory, "Franklin", variant
        )
        base_path = original_path.with_name(
            f"{original_path.stem}-oncogenic{original_path.suffix}"
        )
        panel = page.locator("gnx-oncogenic-classification-app")
        if panel.count() != 1:
            raise RuntimeError("Franklin oncogenic classification panel was unavailable.")
        panel.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        panel.evaluate("el => { el.scrollTop = 0; }")
        categories = panel.locator("gnx-oncogenic-classification-tile")
        if hasattr(categories, "count") and categories.count() == 0:
            return self._capture_franklin_scroll_tiles(
                page,
                panel,
                base_path,
                "Oncogenic classification",
            )

        category_titles = self._wait_for_nonempty_category_titles(
            page, categories
        )
        self._capture_with_incident_retry(
            page,
            base_path,
            lambda: self._capture_franklin_classification_overview(
                page, panel, categories.nth(0), base_path,
                include_gene_header=True,  # Include gene header for first Oncogenic screenshot
            ),
        )
        screenshots = [
            {
                "label": "Oncogenic classification overview",
                "path": str(base_path),
                "url": page.url,
            }
        ]
        for index, title in enumerate(category_titles):
            category = categories.nth(index)
            screenshot_path = base_path.with_name(
                f"{base_path.stem}-evidence-{index + 1:02d}{base_path.suffix}"
            )
            category.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            self._capture_with_incident_retry(
                page,
                screenshot_path,
                lambda category=category, path=screenshot_path: (
                    self._capture_franklin_evidence_box(page, category, path)
                ),
            )
            screenshots.append(
                {
                    "label": f"Oncogenic evidence: {title}",
                    "path": str(screenshot_path),
                    "url": page.url,
                }
            )
        panel.evaluate("el => { el.scrollTop = 0; }")
        return screenshots

    def _capture_franklin_classification_overview(
        self,
        page: Any,
        panel: Any,
        first_category: Any,
        screenshot_path: Path,
        *,
        include_gene_header: bool = False,
    ) -> None:
        """Crop the classification summary before the first evidence category.
        
        Args:
            page: Playwright page object
            panel: The classification panel locator
            first_category: The first evidence category locator
            screenshot_path: Path to save the screenshot
            include_gene_header: If True, capture from top of page to include gene symbol
        """
        panel.evaluate("el => { el.scrollTop = 0; }")
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
        panel_box = panel.bounding_box()
        category_box = first_category.bounding_box()
        if panel_box is None or category_box is None:
            raise RuntimeError("Franklin classification overview was not visible.")
        
        if include_gene_header:
            # Capture from top of page to include gene symbol (e.g., ASXL)
            # Use a fixed top margin to capture the gene header area
            gene_header_height = 120  # Pixels to extend above panel for gene header
            clip_y = max(0, float(panel_box["y"]) - gene_header_height)
            height = float(category_box["y"]) - clip_y
            clip_x = max(0, float(panel_box["x"]))
            clip_width = float(panel_box["width"])
        else:
            clip_y = max(0, float(panel_box["y"]))
            height = float(category_box["y"]) - float(panel_box["y"])
            clip_x = max(0, float(panel_box["x"]))
            clip_width = float(panel_box["width"])
        
        if height <= 1:
            raise RuntimeError("Franklin classification overview could not be cropped.")
        page.screenshot(
            path=str(screenshot_path),
            clip={
                "x": clip_x,
                "y": clip_y,
                "width": clip_width,
                "height": height,
            },
        )

    def _capture_franklin_evidence_box(
        self,
        page: Any,
        category: Any,
        screenshot_path: Path,
    ) -> None:
        """Clone one evidence card outside Franklin's clipping scroll panel."""
        capture_id = "vpm-franklin-" + hashlib.sha256(
            str(screenshot_path).encode("utf-8")
        ).hexdigest()[:16]
        selector = f"[data-vpm-capture='{capture_id}']"
        try:
            category.evaluate(
                """
                (el, captureId) => {
                  const existing = document.querySelector(
                    `[data-vpm-capture='${captureId}']`
                  );
                  if (existing) existing.remove();
                  const rect = el.getBoundingClientRect();
                  const wrapper = document.createElement('div');
                  wrapper.setAttribute('data-vpm-capture', captureId);
                  Object.assign(wrapper.style, {
                    position: 'fixed',
                    left: '0px',
                    top: '0px',
                    width: `${Math.ceil(rect.width)}px`,
                    height: 'auto',
                    overflow: 'visible',
                    background: '#ffffff',
                    zIndex: '2147483647',
                    pointerEvents: 'none'
                  });
                  const clone = el.cloneNode(true);
                  Object.assign(clone.style, {
                    display: 'block',
                    width: '100%',
                    height: 'auto',
                    maxHeight: 'none',
                    overflow: 'visible',
                    margin: '0'
                  });
                  wrapper.appendChild(clone);
                  document.body.appendChild(wrapper);
                  return wrapper.getBoundingClientRect().height > 0;
                }
                """,
                capture_id,
            )
        except (AttributeError, TypeError):
            category.screenshot(path=str(screenshot_path))
            return
        try:
            clone = page.locator(selector)
            clone.wait_for(state="visible", timeout=self.navigation_timeout_ms)
            clone.screenshot(path=str(screenshot_path))
        finally:
            try:
                page.locator(selector).evaluate_all(
                    "nodes => nodes.forEach(node => node.remove())"
                )
            except Exception:
                pass

    def _capture_franklin_scroll_tiles(
        self,
        page: Any,
        scroller: Any,
        base_path: Path,
        label: str,
    ) -> list[dict[str, str]]:
        """Capture unique, contiguous slices from an internal Franklin scroller."""
        scroller.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        dimensions = scroller.evaluate(
            "el => ({clientHeight: el.clientHeight, scrollHeight: el.scrollHeight})"
        )
        box = scroller.bounding_box()
        client_height = int(dimensions.get("clientHeight") or 0)
        scroll_height = int(dimensions.get("scrollHeight") or 0)
        limit = scroll_height
        if box is None or client_height <= 0 or limit <= 0:
            raise RuntimeError("Franklin scrollable evidence panel could not be measured.")

        starts = list(range(0, limit, client_height))
        screenshots: list[dict[str, str]] = []
        try:
            for index, start in enumerate(starts, start=1):
                scroller.evaluate(
                    "(el, top) => { el.scrollTop = top; return el.scrollTop; }",
                    start,
                )
                page.wait_for_timeout(200)
                # Franklin may snap the internal panel to a category boundary
                # after the assignment. Measure the settled position so the
                # last tile ends exactly at the requested evidence boundary.
                actual_scroll = int(
                    scroller.evaluate("el => el.scrollTop") or 0
                )
                current_box = scroller.bounding_box()
                if current_box is None:
                    raise RuntimeError(
                        "Franklin evidence panel became hidden during capture."
                    )
                clip_offset = max(0, start - actual_scroll)
                content_start = max(start, actual_scroll)
                clip_height = min(
                    client_height - clip_offset,
                    limit - content_start,
                )
                if clip_height <= 0:
                    continue
                screenshot_path = (
                    base_path
                    if index == 1
                    else base_path.with_name(
                        f"{base_path.stem}-{index:02d}{base_path.suffix}"
                    )
                )
                page.screenshot(
                    path=str(screenshot_path),
                    clip={
                        "x": current_box["x"],
                        "y": current_box["y"] + clip_offset,
                        "width": current_box["width"],
                        "height": clip_height,
                    },
                )
                screenshots.append(
                    {
                        "label": f"{label} ({index} of {len(starts)})",
                        "path": str(screenshot_path),
                        "url": page.url,
                    }
                )
        finally:
            scroller.evaluate("el => { el.scrollTop = 0; }")
        return screenshots

    @staticmethod
    def _write_audit(evidence: DatabaseEvidence, audit_path: Path) -> None:
        audit_path.write_text(
            json.dumps(asdict(evidence), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _capture_franklin_assessment(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
    ) -> list[dict[str, str]]:
        variant_url = page.url.split("?", 1)[0]
        if not re.search(r"/clinical-db/variant/snp(?:Tumor)?/", variant_url):
            raise ValueError("Franklin did not resolve to a variant page.")
        assessment_url = f"{variant_url}?app=assessment-tools"
        page.goto(
            assessment_url,
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )
        prediction_heading = page.get_by_role(
            "heading", name="Predictions", exact=True
        )
        population_heading = page.get_by_role(
            "heading", name="Population Frequencies", exact=True
        )
        prediction_heading.wait_for(
            state="visible", timeout=self.navigation_timeout_ms
        )
        population_heading.wait_for(
            state="visible", timeout=self.navigation_timeout_ms
        )
        prediction_section = page.locator("div.section").filter(
            has=prediction_heading
        )
        population_section = page.locator("div.section").filter(
            has=population_heading
        )
        if prediction_section.count() != 1 or population_section.count() != 1:
            raise ValueError(
                "Franklin prediction/population evidence panels were ambiguous."
            )
        base_path = self._screenshot_path(
            artifact_directory, "Franklin", variant
        )
        screenshots: list[dict[str, str]] = []
        for label, heading, section, suffix in (
            ("Predictions", prediction_heading, prediction_section, "predictions"),
            (
                "Population frequencies",
                population_heading,
                population_section,
                "population-frequencies",
            ),
        ):
            self._wait_for_franklin_section_stability(page, section)
            heading.evaluate("el => el.scrollIntoView({block: 'start'})")
            page.evaluate("window.scrollTo(0, Math.max(0, window.scrollY - 20))")
            screenshot_path = base_path.with_name(
                f"{base_path.stem}-{suffix}{base_path.suffix}"
            )
            heading_box = heading.bounding_box()
            section_box = section.bounding_box()
            if heading_box is None or section_box is None:
                raise ValueError(f"Franklin {label} panel was not visible.")
            dimensions = section.evaluate(
                """
                el => {
                  const root = el.getBoundingClientRect();
                  const descendants = [el, ...el.querySelectorAll('*')]
                    .map(node => node.getBoundingClientRect())
                    .filter(rect => rect.width > 0 && rect.height > 0);
                  return {
                    width: Math.max(
                      el.scrollWidth,
                      ...descendants.map(rect => rect.right - root.left)
                    ),
                    height: Math.max(
                      el.scrollHeight,
                      ...descendants.map(rect => rect.bottom - root.top)
                    )
                  };
                }
                """
            )
            left = min(float(heading_box["x"]), float(section_box["x"]))
            top = min(float(heading_box["y"]), float(section_box["y"]))
            right = max(
                float(heading_box["x"] + heading_box["width"]),
                float(section_box["x"])
                + max(float(section_box["width"]), float(dimensions.get("width") or 0)),
            )
            bottom = max(
                float(heading_box["y"] + heading_box["height"]),
                float(section_box["y"])
                + max(float(section_box["height"]), float(dimensions.get("height") or 0)),
            )
            clip_top = max(0, top - 64)
            clip_left = max(0, left)
            clip = {
                "x": clip_left,
                "y": clip_top,
                "width": right - clip_left + 16,
                "height": bottom - clip_top,
            }
            self._capture_with_incident_retry(
                page,
                screenshot_path,
                lambda path=screenshot_path, area=clip: page.screenshot(
                    path=str(path), clip=area
                ),
            )
            screenshots.append(
                {"label": label, "path": str(screenshot_path), "url": assessment_url}
            )
        return screenshots

    def _wait_for_franklin_section_stability(self, page: Any, section: Any) -> None:
        """Wait until a dynamic Franklin assessment panel stops changing."""
        previous: tuple[int, int] | None = None
        stable_reads = 0
        for _ in range(8):
            self._check_cancelled()
            state = section.evaluate(
                "el => ({height: el.scrollHeight, textLength: el.innerText.trim().length})"
            )
            current = (
                int(state.get("height") or 0),
                int(state.get("textLength") or 0),
            )
            if current == previous and current[0] > 0 and current[1] > 0:
                stable_reads += 1
                if stable_reads >= 2:
                    return
            else:
                stable_reads = 0
            previous = current
            page.wait_for_timeout(500)

    def _capture_mtbp_variant_screenshot(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
    ) -> Path:
        screenshot_path = self._screenshot_path(
            artifact_directory, "MTBP", variant
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                row, target = self._locate_mtbp_screenshot_target(page, variant)
                target.scroll_into_view_if_needed()
                page.wait_for_timeout(250)
                try:
                    target.screenshot(path=str(screenshot_path))
                except Exception:
                    box = row.bounding_box()
                    if box is None:
                        raise RuntimeError(
                            "MTBP matched the evidence row, but it was hidden for capture."
                        )
                    page.screenshot(path=str(screenshot_path), clip=box)
                validation = self.capture_validator(screenshot_path)
                if not validation.valid:
                    raise IncompleteCaptureError(validation)
                return screenshot_path
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    self._interruptible_page_wait(page, 2_000)
                    continue
        raise IncompleteCaptureError(
            CaptureValidation(
                False, f"mtbp_target:{last_error}", 0, 0, 0.0
            )
        )

    def _locate_mtbp_screenshot_target(
        self, page: Any, variant: VariantRecord
    ) -> tuple[Any, Any]:
        rows = page.locator("table tr")
        row_texts = rows.all_inner_texts()
        alteration_texts: list[str] = []
        for index, text in enumerate(row_texts):
            cells = rows.nth(index).locator("td")
            alteration_texts.append(
                cells.nth(2).inner_text() if cells.count() > 2 else text
            )
        matches = [
            index
            for index, text in enumerate(alteration_texts)
            if _mtbp_screenshot_row_matches(text, variant)
        ]
        visible_matches = [
            index for index in matches if rows.nth(index).is_visible()
        ]
        if len(visible_matches) == 1:
            matches = visible_matches
        if len(matches) != 1:
            gene = (variant.symbol or "").casefold()
            matches = [
                index
                for index, text in enumerate(row_texts)
                if gene and _mtbp_gene_symbol(text).casefold() == gene
            ]
            visible_matches = [
                index for index in matches if rows.nth(index).is_visible()
            ]
            if len(visible_matches) == 1:
                matches = visible_matches
        if len(matches) != 1:
            raise RuntimeError(
                "MTBP could not identify one unique evidence row for the screenshot."
            )
        row = rows.nth(matches[0])
        accordion = row.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' accordion-item ')][1]"
        )
        if accordion.count() == 1 and not row.is_visible():
            toggle = accordion.locator("button.accordion-button")
            if toggle.count() >= 1:
                toggle.first.click()
        row.wait_for(state="visible", timeout=self.navigation_timeout_ms)
        target = accordion if accordion.count() == 1 and accordion.is_visible() else row
        return row, target

    @staticmethod
    def _screenshot_label(database: str) -> str:
        if database == "OncoKB":
            return "Variant overview and mutation effect"
        if database == "Franklin":
            return "Franklin classification and ACMG evidence"
        return "Provider evidence"

    def _next_request_delay_ms(self) -> int:
        if self.request_delay_max_ms <= 0:
            return 0
        return random.randint(self.request_delay_ms, self.request_delay_max_ms)

    def _wait_between_queries(
        self,
        page: Any,
        database: str,
        *,
        progress: Callable[[str], None] | None,
    ) -> None:
        delay_ms = self._next_request_delay_ms()
        if delay_ms <= 0:
            return
        if progress:
            progress(
                f"{database}: safety buffer {delay_ms / 1_000:.1f}s before next variant"
            )
        self._interruptible_page_wait(page, delay_ms)

    def _wait_between_databases(
        self,
        current_database: str,
        next_database: str,
        *,
        progress: Callable[[str], None] | None,
    ) -> None:
        delay_ms = self.provider_switch_delay_ms
        if delay_ms <= 0:
            return
        if progress:
            progress(
                f"Safety buffer {delay_ms / 1_000:.1f}s between "
                f"{current_database} and {next_database}"
            )
        self._interruptible_sleep(delay_ms / 1_000)

    def _check_cancelled(self) -> None:
        if self.stop_requested():
            raise BrowserReviewCancelled("Evidence search stopped by user.")
        self.pause_wait()
        if self.stop_requested():
            raise BrowserReviewCancelled("Evidence search stopped by user.")

    def _interruptible_page_wait(self, page: Any, milliseconds: int) -> None:
        remaining = max(0, int(milliseconds))
        while remaining:
            self._check_cancelled()
            chunk = min(250, remaining)
            page.wait_for_timeout(chunk)
            remaining -= chunk
        self._check_cancelled()

    def _interruptible_sleep(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            self._check_cancelled()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk
        self._check_cancelled()

    def _screenshot_path(
        self, artifact_directory: Path, database: str, variant: VariantRecord
    ) -> Path:
        artifact_directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(
            f"{database}|{variant.symbol}|{variant.hgvsc}|{variant.hgvsp}".encode("utf-8")
        ).hexdigest()[:16]
        return artifact_directory / f"{digest}.png"

    def _login_required(self, database: str, url: str) -> bool:
        lowered = url.lower()
        if database == "COSMIC":
            return "/cosmic/login" in lowered
        if database == "OncoKB":
            return "/login" in lowered
        if database == "Franklin":
            return "/login" in lowered or "auth0" in lowered
        if database == "MTBP":
            return "keycloak" in lowered or "/auth/" in lowered
        return "login" in lowered or "signin" in lowered

    def _session_authenticated(self, database: str, page: Any) -> bool:
        if database == "COSMIC":
            return (
                not self._login_required(database, page.url)
                and page.get_by_role("link", name="log out", exact=False).count() >= 1
            )
        if database == "Franklin":
            return not self._login_required(database, page.url) and page.locator("#email").count() == 0
        if database == "ClinVar":
            return True
        if database == "MTBP":
            return (
                not self._login_required(database, page.url)
                and page.locator("#variant-input").count() == 1
            )
        return not self._login_required(database, page.url)

    def _try_saved_login(self, database: str, page: Any) -> bool:
        if self._session_authenticated(database, page):
            return True
        if database == "Franklin" and self._wait_for_franklin_auth_or_form(page):
            return True
        if database == "COSMIC":
            email, password = self.cosmic_email, self.cosmic_password
            email_selector = "input[placeholder='Registered email address']"
            password_selector = "input[placeholder='COSMIC password']"
            submit_selector = "button[type='submit']"
        elif database == "OncoKB":
            email, password = self.oncokb_email, self.oncokb_password
            email_selector = "input[placeholder='Your institutional email address']"
            password_selector = "input[placeholder='Your password']"
            submit_selector = "button[type='submit']"
        elif database == "Franklin":
            email, password = self.franklin_email, self.franklin_password
            email_selector, password_selector = "#email", "#password"
            submit_selector = "button[type='submit']"
        elif database == "MTBP":
            email, password = self.mtbp_email, self.mtbp_password
            email_selector, password_selector = "#username", "#password"
            submit_selector = "#kc-login"
        else:
            return False
        if not email or not password:
            return False
        try:
            if database == "COSMIC":
                essential_cookies = page.get_by_role(
                    "button", name="Accept essential", exact=True
                )
                if essential_cookies.count() == 1 and essential_cookies.is_visible():
                    essential_cookies.click()
            elif database == "OncoKB":
                reject_cookies = page.get_by_role(
                    "button", name="Reject all", exact=True
                )
                if reject_cookies.count() == 1 and reject_cookies.is_visible():
                    reject_cookies.click()
            page.locator(email_selector).fill(email)
            page.locator(password_selector).fill(password)
            if database == "COSMIC":
                page.get_by_role("button", name="Login", exact=True).click()
            elif database == "OncoKB":
                page.get_by_role("button", name="Sign in", exact=True).click()
            elif database == "Franklin":
                page.locator(submit_selector).filter(has_text=re.compile(r"^SIGN IN$", re.I)).click()
            else:
                page.locator(submit_selector).click()
            page.wait_for_url(
                lambda url: not self._login_required(database, url),
                timeout=60_000,
            )
            if database == "MTBP" and "/analyse" not in page.url:
                self._goto_with_retries(page, self.login_url("MTBP"))
            page.wait_for_timeout(750)
            return self._session_authenticated(database, page)
        except Exception:
            # SPA navigation can detach the sign-in button after the server has
            # already accepted the login. Trust the resulting authenticated URL
            # instead of reporting a false login failure and skipping all queries.
            try:
                return self._session_authenticated(database, page)
            except Exception:
                return False

    def _wait_for_franklin_auth_or_form(self, page: Any) -> bool:
        """Allow Franklin's login route to finish its saved-session redirect."""
        attempts = max(
            1,
            min(5_000, self.navigation_timeout_ms) // 250,
        )
        for _ in range(attempts):
            if self._session_authenticated("Franklin", page):
                return True
            if (
                page.locator("#email").count() == 1
                and page.locator("#password").count() == 1
            ):
                return False
            page.wait_for_timeout(250)
        return self._session_authenticated("Franklin", page)

    def _wait_for_oncokb_result(self, page: Any) -> None:
        """Wait for OncoKB's client-rendered variant evidence, not network idle."""
        attempts = max(1, self.navigation_timeout_ms // 500)
        for _ in range(attempts):
            if self._login_required("OncoKB", page.url):
                return
            body_text = page.locator("body").inner_text()
            if "Variant Overview" in body_text and "Mutation Effect" in body_text:
                return
            if "Page not found" in body_text or "An error has occurred" in body_text:
                return
            page.wait_for_timeout(500)
        raise TimeoutError("OncoKB did not finish rendering the variant result.")

    def _browser_api(self):
        try:
            from archer_processor.services.edge_cdp import (
                EdgeCdpError,
                EdgeCdpTimeout,
                sync_edge_cdp,
            )
        except ImportError as exc:
            raise BrowserAutomationUnavailable(
                "Browser review requires the pure-Python websocket-client package. "
                "Reinstall Archer Prosess packages."
            ) from exc
        return sync_edge_cdp, EdgeCdpError, EdgeCdpTimeout

    def _validate_database(self, database: str) -> None:
        if database not in BROWSER_DATABASES:
            raise ValueError(f"Unsupported browser database: {database}")

    @staticmethod
    def variant_key(variant: VariantRecord) -> str:
        return f"{variant.sample}|{variant.hgvsc}"


def _cosmic_identifier(value: str | None) -> str:
    match = re.search(r"\bCOS(?:M|V)\d+\b", value or "", re.IGNORECASE)
    return match.group(0).upper() if match else ""


def _cosmic_numeric_id(value: str | None) -> str:
    identifier = _cosmic_identifier(value)
    match = re.search(r"\d+", identifier)
    return match.group(0) if match else ""


def _cosmic_source_url(current_url: str, cosmic_id: str | None) -> str:
    """Return the exact resolved COSMIC page that was validated and captured.

    COSMIC's GRCh37 links can require extra query parameters such as ``cosm``,
    ``genome`` and ``trans``. Rebuilding the URL from only ``id`` and ``merge``
    can therefore turn a working page into a "mutation not found" link.
    """
    return current_url


def parse_oncokb_page(
    body_text: str, variant: VariantRecord, url: str
) -> DatabaseEvidence:
    oncogenicity = _after_heading(body_text, "Oncogenicity")
    biological_effect = _after_heading(body_text, "Biological Effect")
    overview = _between(body_text, "Variant Overview", "Mutation Effect")
    page_identity = f"{variant.symbol} {_protein_change(variant.hgvsp)}".strip()
    if not oncogenicity and "Variant Overview" not in body_text:
        return DatabaseEvidence(
            "OncoKB", "not_found", f"No OncoKB web result for {page_identity}.",
            accession=page_identity, url=url,
        )
    parts = [f"oncogenic={oncogenicity or 'unknown'}"]
    if biological_effect:
        parts.append(f"mutation_effect={biological_effect}")
    if overview:
        parts.append("overview=" + " ".join(overview.split())[:500])
    return DatabaseEvidence(
        database="OncoKB",
        status="found",
        summary="; ".join(parts),
        accession=page_identity,
        clinical_significance=oncogenicity,
        url=url,
        raw={"oncogenicity": oncogenicity, "biological_effect": biological_effect},
    )


def parse_franklin_page(
    body_text: str, variant: VariantRecord, url: str
) -> DatabaseEvidence:
    query = _franklin_search_query(variant)
    verification = _franklin_identity(body_text, url, variant)
    verification_raw = {
        "accepted": verification.accepted,
        "basis": verification.basis,
        "reason": verification.reason,
        "requested": asdict(genomic_identity(variant))
        if genomic_identity(variant)
        else None,
        "returned": asdict(verification.returned)
        if verification.returned
        else None,
    }
    if not verification.accepted:
        return DatabaseEvidence(
            "Franklin",
            "identity_mismatch",
            f"Franklin returned a different variant than {query}; result was not imported.",
            accession=query,
            url=url,
            raw={"identity_verification": verification_raw},
        )
    classification = _after_heading(body_text, "Suggested classification")
    if not classification:
        return DatabaseEvidence(
            "Franklin", "not_found", f"No Franklin web result for {query}.",
            accession=query, url=url,
        )
    return DatabaseEvidence(
        database="Franklin",
        status="found",
        summary=f"classification={classification};",
        accession=query,
        clinical_significance=classification,
        url=url,
        raw={
            "classification": classification,
            "identity_verification": verification_raw,
        },
    )


def parse_mtbp_report(
    body_text: str,
    report_rows: list[dict[str, Any]],
    variants: Iterable[VariantRecord],
    url: str,
    *,
    cancer_type: str,
) -> dict[str, DatabaseEvidence]:
    """Map the MTBP alteration-centric table to submitted variants.

    Matching is deliberately strict. A gene-only match is accepted only when
    the report contains one row for that gene and MTBP displays no variant
    identity for it; ambiguous rows and identity mismatches fail closed.
    """
    metadata = _mtbp_report_metadata(body_text, cancer_type)
    results: dict[str, DatabaseEvidence] = {}
    for variant in variants:
        key = BrowserReviewService.variant_key(variant)
        gene_candidates = [
            row for row in report_rows
            if _mtbp_gene_symbol(str(row.get("gene", ""))).casefold()
            == variant.symbol.casefold()
        ]
        candidates = list(gene_candidates)
        match_basis = "gene"
        expected_protein = _mtbp_normalized_protein(_protein_change(variant.hgvsp))
        expected_cdna = _cdna_change(variant.hgvsc).casefold()
        if expected_protein:
            protein_matches = [
                row for row in candidates
                if expected_protein in _mtbp_proteins(str(row.get("identity_text", "")))
                or expected_protein in _mtbp_proteins(str(row.get("alteration", "")))
            ]
            candidates = protein_matches
            match_basis = "protein"
        elif expected_cdna:
            cdna_matches = [
                row for row in candidates
                if expected_cdna in re.sub(r"\s+", "", str(row.get("identity_text", ""))).casefold()
            ]
            if cdna_matches:
                candidates = cdna_matches
                match_basis = "cdna"
            elif len(gene_candidates) == 1 and not _mtbp_row_has_variant_identity(
                gene_candidates[0]
            ):
                candidates = gene_candidates
                match_basis = "unique_gene_without_reported_identity"
            else:
                candidates = []

        if len(candidates) != 1:
            status = "not_found" if not candidates else "ambiguous_result"
            detail = "no matching row" if not candidates else f"{len(candidates)} possible rows"
            results[key] = DatabaseEvidence(
                "MTBP",
                status,
                f"MTBP report returned {detail} for {_mtbp_variant_query(variant)}.",
                accession=_mtbp_variant_query(variant),
                url=url,
                raw={
                    **metadata,
                    "candidate_count": len(candidates),
                    "gene_candidate_count": len(gene_candidates),
                    "match_basis": match_basis,
                },
            )
            continue

        row = candidates[0]
        classification = re.sub(
            r":\s*\d+\s*$", "", str(row.get("section", ""))
        ).strip() or "Reported alteration"
        functional_evidence = " ".join(
            str(row.get("functional_evidence", "")).split()
        )
        biomarkers = " ".join(str(row.get("biomarkers", "")).split())
        evidence_categories = list(
            dict.fromkeys(
                re.findall(
                    r"Evidence\s+[A-Z](?:\s*\([^)]+\))?",
                    functional_evidence,
                    flags=re.IGNORECASE,
                )
            )
        )
        actionability_tiers = list(
            dict.fromkeys(
                match.strip()
                for match in re.findall(
                    r"Tier\s+\d[^\n]*(?=(?:\s+Tier\s+\d)|$)",
                    str(row.get("biomarkers", "")),
                    flags=re.IGNORECASE,
                )
            )
        )
        summary_parts = [f"functional_relevance={classification}"]
        if evidence_categories:
            summary_parts.append("evidence=" + ", ".join(evidence_categories))
        if biomarkers:
            summary_parts.append("biomarkers=" + biomarkers[:650])
        if metadata.get("pipeline_version"):
            summary_parts.append(f"pipeline={metadata['pipeline_version']}")
        results[key] = DatabaseEvidence(
            database="MTBP",
            status="found",
            summary="; ".join(summary_parts),
            accession=_mtbp_variant_query(variant),
            clinical_significance=classification,
            url=url,
            raw={
                **metadata,
                "gene": row.get("gene", ""),
                "gene_info": row.get("gene_info", ""),
                "alteration": row.get("alteration", ""),
                "functional_relevance": classification,
                "functional_evidence": functional_evidence,
                "actionability_tiers": actionability_tiers,
                "biomarkers": biomarkers,
                "source_links": list(dict.fromkeys(row.get("source_links", []))),
                "match_basis": match_basis,
            },
        )
    return results


def _mtbp_report_metadata(body_text: str, cancer_type: str) -> dict[str, Any]:
    def capture(label: str) -> str:
        match = re.search(
            rf"{re.escape(label)}\s*:?\s*([^\n•]+)", body_text, flags=re.IGNORECASE
        )
        return match.group(1).strip() if match else ""

    versions: dict[str, str] = {}
    for source in (
        "VEP", "Transvar", "BRCA-Exchange", "CIViC", "OncoKB", "CGC",
        "gnomAD", "1000 Genomes", "CADD", "MSKCC-hotspots",
        "MSKCC-hotspots-3D", "OncoTree", "Human genome",
    ):
        value = capture(source)
        if value:
            versions[source] = value
    return {
        "analysis_run_date": capture("Analysis run date"),
        "pipeline_version": capture("Pipeline version"),
        "cancer_type": cancer_type,
        "database_versions": versions,
        "research_only": True,
    }


def _after_heading(text: str, heading: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if line.casefold() == heading.casefold():
            return lines[index + 1]
    return ""


def _between(text: str, start_heading: str, end_heading: str) -> str:
    match = re.search(
        re.escape(start_heading) + r"\s+(.*?)\s+" + re.escape(end_heading),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _protein_change(value: str) -> str:
    clean = (value or "").split(":", 1)[-1].replace("p.", "").replace("(", "").replace(")", "").strip()
    match = re.fullmatch(r"([A-Z][a-z]{2})(\d+)([A-Z][a-z]{2}|Ter|\*)", clean)
    if not match:
        return clean
    reference, position, alternate = match.groups()
    reference_short = _AMINO_ACID_3_TO_1.get(reference)
    alternate_short = "*" if alternate in {"Ter", "*"} else _AMINO_ACID_3_TO_1.get(alternate)
    return f"{reference_short}{position}{alternate_short}" if reference_short and alternate_short else clean


def _cdna_change(value: str) -> str:
    return (value or "").split(":", 1)[-1].strip()


def _first_nonempty_line(value: str) -> str:
    return next((line.strip() for line in (value or "").splitlines() if line.strip()), "")


def _franklin_query(variant: VariantRecord) -> str:
    queries = _franklin_queries(variant)
    return queries[-1] if queries else ""


def _franklin_queries(variant: VariantRecord) -> list[str]:
    queries: list[str] = []
    cdna = _cdna_change(variant.hgvsc)
    if variant.symbol and cdna:
        queries.append(f"{variant.symbol}:{cdna}")
    identity = genomic_identity(variant)
    if identity:
        queries.append(
            f"chr{identity.chromosome}-{identity.position} "
            f"{identity.reference}>{identity.alternate}"
        )
    if not queries and variant.hgvsc:
        queries.append(variant.hgvsc)
    return list(dict.fromkeys(queries))


def _franklin_search_query(variant: VariantRecord) -> str:
    queries = _franklin_queries(variant)
    return queries[0] if queries else ""


def _franklin_page_matches(body_text: str, variant: VariantRecord) -> bool:
    return _franklin_identity(body_text, "", variant).accepted


def _franklin_identity(
    body_text: str,
    url: str,
    variant: VariantRecord,
) -> IdentityVerification:
    compact = re.sub(r"\s+", "", body_text or "").casefold()
    if variant.symbol and variant.symbol.casefold() not in compact:
        return IdentityVerification(False, "none", "Gene symbol did not match.")
    cdna = _cdna_change(variant.hgvsc)
    if cdna and cdna.casefold() in compact:
        return IdentityVerification(
            True,
            "exact_transcript",
            "Transcript cDNA matched exactly.",
        )
    expected = genomic_identity(variant)
    returned = _franklin_genomic_identity(body_text, url)
    if expected and returned:
        if expected == returned:
            return IdentityVerification(
                True,
                "grch37_genomic",
                "GRCh37 chromosome, position, reference, and alternate matched.",
                returned,
            )
        return IdentityVerification(
            False,
            "grch37_genomic",
            "Returned GRCh37 genomic identity differed from the requested variant.",
            returned,
        )
    protein = _mtbp_normalized_protein(_protein_change(variant.hgvsp))
    if not cdna and protein and protein in _mtbp_proteins(body_text):
        return IdentityVerification(True, "exact_protein", "Protein matched exactly.")
    return IdentityVerification(False, "none", "No exact variant identity matched.")


def _franklin_genomic_identity(
    body_text: str,
    url: str,
) -> GenomicIdentity | None:
    combined = f"{body_text}\n{url}"
    match = re.search(
        r"chr(?P<chromosome>[0-9]{1,2}|X|Y|M|MT)"
        r"[-:](?P<position>\d+)"
        r"(?:\s+|[-:])(?P<reference>[ACGTN]+)"
        r"\s*(?:>|/|-)\s*(?P<alternate>[ACGTN]+)",
        combined,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    chromosome = match.group("chromosome").upper()
    if chromosome == "MT":
        chromosome = "M"
    return GenomicIdentity(
        "GRCh37",
        chromosome,
        int(match.group("position")),
        match.group("reference").upper(),
        match.group("alternate").upper(),
    )


def _franklin_quota_message(body_text: str) -> str:
    lowered = (body_text or "").casefold()
    quota_markers = (
        "free search limit",
        "free searches limit",
        "search limit reached",
        "reached your search limit",
        "upgrade to continue",
    )
    if any(marker in lowered for marker in quota_markers):
        return (
            "Franklin's anonymous search allowance is exhausted. "
            "Save Franklin login credentials in Settings and retry."
        )
    return ""


def _mtbp_variant_query(variant: VariantRecord) -> str:
    cdna = _cdna_change(variant.hgvsc)
    cdna_accession = (variant.hgvsc or "").split(":", 1)[0]
    if cdna and ":" in (variant.hgvsc or "") and _mtbp_accession(cdna_accession):
        return f"{cdna_accession}:{cdna}"
    protein = _protein_change(variant.hgvsp)
    protein_accession = (variant.hgvsp or "").split(":", 1)[0]
    if protein and ":" in (variant.hgvsp or "") and _mtbp_accession(protein_accession):
        return f"{protein_accession}:p.{protein}"
    transcript = (variant.transcript or "").strip()
    if transcript and cdna and _mtbp_accession(transcript):
        return f"{transcript}:{cdna}"
    if transcript and protein and _mtbp_accession(transcript):
        return f"{transcript}:p.{protein}"
    if variant.symbol and cdna:
        return f"{variant.symbol}:{cdna}"
    if variant.symbol and protein:
        return f"{variant.symbol}:p.{protein}"
    if variant.genomic_location and variant.ref_allele and variant.alt_allele:
        try:
            chromosome, position = variant.genomic_location.split(":", 1)
            chromosome = chromosome if chromosome.lower().startswith("chr") else f"chr{chromosome}"
            ref = variant.ref_allele.split("/", 1)[0]
            alt = variant.alt_allele.split("/", 1)[-1]
            return f"{chromosome}:g.{position.split('-', 1)[0]}{ref}>{alt}"
        except ValueError:
            return ""
    return ""


def _mtbp_genomic_query(variant: VariantRecord) -> str:
    """Convert an Archer GRCh37 VCF-style allele into genomic HGVS.

    Archer represents indels with an anchored reference base. MTBP expects the
    inserted/deleted sequence itself in HGVS, so a simple ``REF>ALT`` string is
    only correct for substitutions.
    """
    location = re.sub(r"\s+", "", variant.genomic_location or "")
    match = re.fullmatch(
        r"(?:chr)?(?P<chromosome>[0-9]{1,2}|X|Y|M|MT):(?P<position>\d+)(?:-\d+)?",
        location,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    chromosome = match.group("chromosome").upper()
    if chromosome == "MT":
        chromosome = "M"
    position = int(match.group("position"))
    ref = re.sub(r"\s+", "", variant.ref_allele or "").upper()
    alt = re.sub(r"\s+", "", variant.alt_allele or "").upper()
    if (
        not ref
        or not alt
        or ref == alt
        or not re.fullmatch(r"[ACGTN]+", ref)
        or not re.fullmatch(r"[ACGTN]+", alt)
    ):
        return ""

    prefix = f"chr{chromosome}:g."
    if len(ref) == len(alt) == 1:
        return f"{prefix}{position}{ref}>{alt}"
    if alt.startswith(ref):
        inserted = alt[len(ref):]
        left = position + len(ref) - 1
        return f"{prefix}{left}_{left + 1}ins{inserted}"
    if ref.startswith(alt):
        start = position + len(alt)
        end = position + len(ref) - 1
        coordinate = str(start) if start == end else f"{start}_{end}"
        return f"{prefix}{coordinate}del"

    end = position + len(ref) - 1
    coordinate = str(position) if position == end else f"{position}_{end}"
    return f"{prefix}{coordinate}delins{alt}"


def _mtbp_accession(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:NM|NR|NP|XM|XR|XP)_\d+(?:\.\d+)?|ENS(?:T|P)\d+(?:\.\d+)?",
            (value or "").strip(),
            flags=re.IGNORECASE,
        )
    )


def _mtbp_unmapped_queries(body_text: str) -> list[str]:
    match = re.search(
        r"following mutation\(s\) cannot be mapped to genomic coordinates:\s*"
        r"(.*?)\s*(?:Please check|--Please correct)",
        body_text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    return [
        item.strip().rstrip(".")
        for item in re.split(r",\s*(?=[A-Za-z0-9_.-]+:)", match.group(1).strip())
        if item.strip()
    ]


def _mtbp_query_rejected(query: str, rejected: list[str]) -> bool:
    normalized = re.sub(r"\s+", "", query).casefold()
    coordinate = normalized.split(":", 1)[-1]
    for item in rejected:
        rejected_normalized = re.sub(r"\s+", "", item).casefold()
        if normalized == rejected_normalized:
            return True
        if coordinate and coordinate == rejected_normalized.split(":", 1)[-1]:
            return True
    return False


def _mtbp_normalized_protein(value: str) -> str:
    clean = (value or "").replace("p.", "").replace("(", "").replace(")", "").strip()
    match = re.fullmatch(r"([A-Z][a-z]{2})(\d+)([A-Z][a-z]{2}|Ter|\*)", clean)
    if match:
        reference, position, alternate = match.groups()
        reference = _AMINO_ACID_3_TO_1.get(reference, reference)
        alternate = "*" if alternate in {"Ter", "*"} else _AMINO_ACID_3_TO_1.get(alternate, alternate)
        clean = f"{reference}{position}{alternate}"
    return clean.casefold()


def _mtbp_gene_symbol(value: str) -> str:
    match = re.search(r"[A-Za-z0-9-]+", value or "")
    return match.group(0) if match else ""


def _mtbp_row_has_variant_identity(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(row.get(field, "")) for field in ("identity_text", "alteration")
    )
    if _mtbp_proteins(text):
        return True
    return bool(re.search(r"\b[cnrmg]\.\s*[-+*0-9]", text, flags=re.IGNORECASE))


def _mtbp_proteins(text: str) -> set[str]:
    tokens = re.findall(r"p\.\(?([A-Za-z0-9_*?]+)\)?", text or "")
    return {_mtbp_normalized_protein(token) for token in tokens}


def _mtbp_screenshot_row_matches(text: str, variant: VariantRecord) -> bool:
    compact = re.sub(r"\s+", "", text or "").casefold()
    if not variant.symbol or variant.symbol.casefold() not in compact:
        return False
    expected_protein = _mtbp_normalized_protein(_protein_change(variant.hgvsp))
    if expected_protein:
        return expected_protein in _mtbp_proteins(text)
    expected_cdna = _cdna_change(variant.hgvsc).casefold()
    return bool(expected_cdna and expected_cdna in compact)


def _review_query(variant: VariantRecord) -> str:
    return " ".join(
        value for value in [variant.symbol, variant.hgvsc, variant.hgvsp, variant.genomic_location]
        if value
    )
