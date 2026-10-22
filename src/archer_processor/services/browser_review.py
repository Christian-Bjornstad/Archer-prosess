from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from PIL import Image, PngImagePlugin

from archer_processor.core.highlights import is_automatic_database_skip
from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.core.variant_key_guard import validate_variant_keys
from archer_processor.services.genomic_notation import format_mtbp_grch37
from archer_processor.services.mtbp_report_dom import MTBP_TABLE_ROWS_JS
from archer_processor.services.capture_layout import expanded_capture_layout
from archer_processor.services.provider_failures import (
    ProviderFailureKind,
    ProviderLookupError,
)
from archer_processor.services.evidence_audit import (
    MTBP_MATCHING_REVISION,
    is_completed_evidence,
    persist_evidence_result,
    write_evidence_audit,
)
from archer_processor.services.browser_popups import dismiss_known_overlays, dismiss_franklin_onboarding
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


class _ClinVarRenderingTimeout(TimeoutError):
    def __init__(self, diagnostics: dict[str, object]) -> None:
        super().__init__("ClinVar did not finish rendering the website result.")
        self.diagnostics = {"render_diagnostics": diagnostics}


class _OncoKBRenderingTimeout(TimeoutError):
    def __init__(self, diagnostics: dict[str, object]) -> None:
        super().__init__("OncoKB did not finish rendering the variant result.")
        self.diagnostics = {"render_diagnostics": diagnostics}


BROWSER_DATABASES = ("COSMIC", "OncoKB", "Franklin", "ClinVar", "MTBP")
MTBP_REPORTS_URL = "https://mtbp.org/patients/"
MTBP_ARCHER_REPORT_LIMIT = 1
MTBP_DELETE_SETTLE_MS = 5_000
MTBP_DELETE_VERIFY_ATTEMPTS = 3

LOGIN_URLS = {
    "ClinVar": "https://www.ncbi.nlm.nih.gov/clinvar/",
    "COSMIC": "https://cancer.sanger.ac.uk/cosmic/login",
    "OncoKB": "https://www.oncokb.org/login",
    "Franklin": "https://franklin.genoox.com/login",
    "MTBP": "https://mtbp.org/analyse/",
}

FRANKLIN_HOME_URL = "https://franklin.genoox.com/clinical-db/home"
PROVIDER_SWITCH_DELAY_MS = 3_000
# COSMIC uses its own short buffer between variant lookups: queries resolve
# fast and the site has no per-query throttle at this cadence.
COSMIC_REQUEST_DELAY_MS = 3_000
COSMIC_REQUEST_DELAY_MAX_MS = 8_000
DEFAULT_REQUEST_DELAY_MS = 3_000
DEFAULT_REQUEST_DELAY_MAX_MS = 8_000

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
    """Run one provider lane in an isolated persistent Edge profile.

    Passwords are entered directly into each provider's page. The application
    retains only the provider browser profile (cookies/local storage), never the
    password itself. Calls within a lane are serial; the GUI may overlap lanes
    for different providers while keeping patients and per-provider variants serial.
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
        franklin_attempts: int = 1,
        request_delay_ms: int = DEFAULT_REQUEST_DELAY_MS,
        request_delay_max_ms: int | None = DEFAULT_REQUEST_DELAY_MAX_MS,
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
        self.login_confirmed = False
        self._cosmic_cache: dict[tuple[str, ...], DatabaseEvidence] = {}
        self._result_checkpoint: Callable[[dict[str, list[DatabaseEvidence]]], None] | None = None
        self._mtbp_rejected_transcript_queries: set[str] = set()
        self._variant_result_callback: Callable[[VariantRecord, DatabaseEvidence], None] | None = None

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

    def _launch_browser_context(self, runtime, database: str, **options):
        """Keep startup diagnostics at the boundary, before any site is read."""
        from archer_processor.services.edge_cdp import EdgeCdpError, EdgeCdpTimeout

        self._validate_database(database)
        profile = self.profile_root / database.lower().replace(" ", "-")
        try:
            return runtime.chromium.launch_persistent_context(
                str(self.profile_directory(database)), channel=self.channel,
                headless=False, accept_downloads=options.pop("accept_downloads", True),
                **options,
            )
        except Exception as exc:
            detail = (
                str(exc) if isinstance(exc, (EdgeCdpError, EdgeCdpTimeout, OSError))
                else "Unexpected startup failure"
            )
            raise BrowserAutomationUnavailable(
                f"Could not start Microsoft Edge for {database}.\n"
                f"Profile: {profile}\nActual startup error: {type(exc).__name__}: {detail}"
            ) from exc

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
            candidates = _oncokb_query_urls(variant)
            return candidates[0] if candidates else ""
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
        self.login_confirmed = False
        sync_browser, browser_error, _ = self._browser_api()
        with sync_browser() as runtime:
            context = self._launch_browser_context(
                runtime,
                database,
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                self.login_url(database),
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            page.bring_to_front()
            if self._try_saved_login(database, page):
                context.close()
                self.login_confirmed = True
                return f"{database} signed in using Windows Credential Manager."
            deadline = datetime.now(timezone.utc).timestamp() + maximum_minutes * 60
            try:
                while context.pages and datetime.now(timezone.utc).timestamp() < deadline:
                    active_page = context.pages[0]
                    if self._session_authenticated(database, active_page):
                        self.login_confirmed = True
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
        prior_evidence: dict[str, list[DatabaseEvidence]] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        variant_list = list(variants)
        validate_variant_keys(variant_list)
        searchable_variants = [
            variant
            for variant in variant_list
            if not is_automatic_database_skip(variant)
        ]
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
                    for variant in searchable_variants
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
            provider_started_at = time.monotonic()
            provider_directory = artifact_root / database.lower().replace(" ", "-")
            variants_by_key = {
                self.variant_key(variant): variant for variant in pending_variants
            }
            published: dict[str, DatabaseEvidence] = {}

            def persist_result(variant: VariantRecord, evidence: DatabaseEvidence) -> None:
                query_attempts = evidence.raw.get("query_attempts", [])
                try:
                    write_evidence_audit(
                        provider_directory,
                        database,
                        variant,
                        evidence,
                        query_attempts=(
                            query_attempts if isinstance(query_attempts, list) else []
                        ),
                        duration_seconds=time.monotonic() - provider_started_at,
                    )
                except OSError as exc:
                    if progress:
                        progress(
                            f"{database}: audit could not be written; result remains "
                            f"in memory for workbook checkpoint ({exc})"
                        )

            def publish_result(variant: VariantRecord, evidence: DatabaseEvidence) -> None:
                key = self.variant_key(variant)
                persist_result(variant, evidence)
                published[key] = evidence
                if checkpoint:
                    checkpoint(deepcopy({key: [evidence]}))

            previous_checkpoint = self._result_checkpoint
            self._result_checkpoint = checkpoint
            previous_callback = self._variant_result_callback
            self._variant_result_callback = publish_result
            try:
                database_results = self._search_database(
                    database,
                    pending_variants,
                    provider_directory,
                    progress=provider_progress,
                    prior_evidence=prior_evidence,
                )
            finally:
                self._variant_result_callback = previous_callback
                self._result_checkpoint = previous_checkpoint
            provider_duration = time.monotonic() - provider_started_at
            unpublished_results = {
                key: evidence for key, evidence in database_results.items()
                if published.get(key) is not evidence
            }
            for key, evidence in unpublished_results.items():
                variant = variants_by_key.get(key)
                if variant is not None:
                    persist_result(variant, evidence)
            self._report_provider_results(
                database,
                pending_variants,
                database_results,
                duration_seconds=provider_duration,
                progress=provider_progress,
            )
            for key, evidence in database_results.items():
                results[key].append(evidence)
            if checkpoint and unpublished_results:
                checkpoint(
                    deepcopy({
                        key: [evidence]
                        for key, evidence in unpublished_results.items()
                    })
                )
            if activity:
                activity(database, "Provider finished")
            self._check_cancelled()
            if database_index < len(jobs) - 1:
                self._wait_between_databases(
                    database,
                    jobs[database_index + 1][0],
                    progress=provider_progress,
                )
        return results

    def _publish_variant_result(
        self, variant: VariantRecord, evidence: DatabaseEvidence
    ) -> None:
        if self._variant_result_callback is not None:
            self._variant_result_callback(variant, evidence)

    def _report_provider_results(
        self,
        database: str,
        variants: list[VariantRecord],
        results: dict[str, DatabaseEvidence],
        *,
        duration_seconds: float,
        progress: Callable[[str], None] | None,
    ) -> None:
        if progress is None:
            return
        status_counts: dict[str, int] = {}
        retryable_count = 0
        for variant in variants:
            evidence = results.get(self.variant_key(variant))
            if evidence is None:
                continue
            status = evidence.status.strip().casefold() or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
            retryable = not is_completed_evidence(evidence)
            retryable_count += int(retryable)
            fields = [
                "RESULT",
                f"source={database}",
                f"variant={_log_field(evidence.accession or _review_query(variant))}",
                f"status={_log_field(status)}",
                f"retryable={'yes' if retryable else 'no'}",
            ]
            failure_stage = str(evidence.raw.get("failure_stage") or "").strip()
            if failure_stage:
                fields.append(f"stage={_log_field(failure_stage)}")
            diagnostics = evidence.raw.get("render_diagnostics")
            if isinstance(diagnostics, dict):
                attempts = evidence.raw.get("query_attempts", [])
                fields.append(f"attempts={len(attempts) if isinstance(attempts, list) else 0}")
                page_type = diagnostics.get("page_type")
                if page_type in {"record", "search", "variant"}:
                    fields.append(f"page_type={page_type}")
                for name in (
                    "record_metadata", "hgvs_title", "genomic_location",
                    "variant_overview", "mutation_effect",
                ):
                    if name in diagnostics:
                        fields.append(f"{name}={'yes' if diagnostics[name] else 'no'}")
                if "result_links" in diagnostics:
                    fields.append(f"result_links={int(diagnostics['result_links'] or 0)}")
            if evidence.summary:
                fields.append(f"reason={_log_field(evidence.summary, maximum=240)}")
            if database == "MTBP" and evidence.status != "found":
                for field, label_name in (
                    ("report_row_count", "report_rows"),
                    ("gene_candidate_count", "gene_rows"),
                    ("candidate_count", "matching_rows"),
                    ("match_basis", "match_basis"),
                ):
                    if field in evidence.raw:
                        fields.append(f"{label_name}={_log_field(str(evidence.raw[field]))}")
            progress(" | ".join(fields))

        count_fields = [
            f"{status}={count}"
            for status, count in sorted(status_counts.items())
        ]
        progress(
            " | ".join(
                [
                    "SUMMARY",
                    f"source={database}",
                    f"total={len(results)}",
                    *count_fields,
                    f"retryable={retryable_count}",
                    f"duration={max(0.0, duration_seconds):.1f}s",
                ]
            )
        )

    def _search_database(
        self,
        database: str,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
        prior_evidence: dict[str, list[DatabaseEvidence]] | None = None,
    ) -> dict[str, DatabaseEvidence]:
        self._validate_database(database)
        if database == "MTBP":
            return self._search_mtbp(
                variants,
                artifact_directory,
                progress=progress,
                prior_evidence=prior_evidence,
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
            context = self._launch_browser_context(
                runtime,
                database,
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                background=self.browser_background,
            )
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
                        self._publish_variant_result(variant, results[key])
                        continue
                    if progress:
                        progress(
                            f"{database} browser lookup {index}/{len(variants)}: "
                            f"{variant.symbol} {_protein_change(variant.hgvsp) or _cdna_change(variant.hgvsc)}"
                        )
                    try:
                        if database == "OncoKB":
                            results[key] = self._lookup_oncokb_variant(
                                page, variant, artifact_directory
                            )
                        else:
                            page.goto(
                                query_url,
                                wait_until="domcontentloaded",
                                timeout=self.navigation_timeout_ms,
                            )
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
                    self._publish_variant_result(variant, results[key])
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

    def _lookup_oncokb_variant(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
    ) -> DatabaseEvidence:
        attempts: list[str] = []
        transcript_mismatch = False
        last_result = DatabaseEvidence(
            "OncoKB", "invalid_query", "Cannot build an OncoKB web query."
        )
        for query_url in _oncokb_query_urls(variant):
            attempts.append(query_url)
            last_result = self._lookup_oncokb_url(
                page, variant, query_url, artifact_directory
            )
            last_result.raw["query_attempts"] = list(attempts)
            failure_kind = str(last_result.raw.get("failure_kind") or "")
            transcript_mismatch = transcript_mismatch or (
                failure_kind == ProviderFailureKind.IDENTITY_MISMATCH.value
            )
            if last_result.status == "found":
                return last_result
            # A stalled protein-route result may still resolve through the exact
            # genomic route. Never use alternative queries to bypass a login.
            if (
                last_result.status == "error"
                and failure_kind == ProviderFailureKind.TRANSIENT.value
                and last_result.raw.get("failure_stage") == "result_rendering"
                and "/gene/" in query_url
            ):
                continue
            if (
                last_result.status not in {"not_found", "identity_mismatch"}
                and failure_kind != ProviderFailureKind.IDENTITY_MISMATCH.value
            ):
                return last_result
        if transcript_mismatch:
            return DatabaseEvidence(
                "OncoKB",
                "manual_review",
                "OncoKB transcript identity differed and the GRCh37 genomic "
                "website fallback did not produce a verified variant result.",
                accession=_review_query(variant),
                url=last_result.url,
                raw={
                    "failure_kind": ProviderFailureKind.IDENTITY_MISMATCH.value,
                    "query_attempts": attempts,
                },
            )
        return last_result

    def _lookup_oncokb_url(
        self,
        page: Any,
        variant: VariantRecord,
        query_url: str,
        artifact_directory: Path,
    ) -> DatabaseEvidence:
        page.goto(
            query_url,
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )
        try:
            self._wait_for_oncokb_result(page)
        except ProviderLookupError as exc:
            return DatabaseEvidence(
                "OncoKB", exc.kind.value, str(exc), accession=_review_query(variant),
                url=page.url, raw={"failure_kind": exc.kind.value, **exc.diagnostics},
            )
        except TimeoutError as exc:
            artifact_directory.mkdir(parents=True, exist_ok=True)
            base_path = self._screenshot_path(artifact_directory, "OncoKB", variant)
            diagnostic_path = base_path.with_name(f"{base_path.stem}-render-timeout.png")
            visible_text = ""
            capture_error = ""
            try:
                visible_text = page.locator("body").inner_text()[:12_000]
            except Exception as text_exc:
                capture_error = f"text: {text_exc}"
            try:
                page.screenshot(path=str(diagnostic_path), full_page=False)
            except Exception as screenshot_exc:
                capture_error = " ; ".join(
                    part for part in (capture_error, f"screenshot: {screenshot_exc}") if part
                )
                diagnostic_path = Path()
            return DatabaseEvidence(
                "OncoKB",
                "error",
                f"OncoKB did not finish rendering the variant result ({exc}).",
                accession=_review_query(variant),
                url=page.url,
                raw={
                    "failure_kind": ProviderFailureKind.TRANSIENT.value,
                    "failure_stage": "result_rendering",
                    **getattr(exc, "diagnostics", {}),
                    "visible_text_preview": visible_text,
                    "diagnostic_screenshot": str(diagnostic_path),
                    "diagnostic_capture_error": capture_error,
                },
            )
        return self._capture_result("OncoKB", variant, page, artifact_directory)

    def _search_cosmic(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Capture the licensed COSMIC mutation page by Archer COSMICID."""
        searchable_variants = [
            variant for variant in variants if _cosmic_identifiers(variant.cosmic_id)
        ]
        results: dict[str, DatabaseEvidence] = {
            self.variant_key(variant): DatabaseEvidence(
                "COSMIC",
                "not_applicable",
                "ingen COSMIC-ID",
                accession=variant.cosmic_id,
                raw={
                    "failure_kind": ProviderFailureKind.NOT_FOUND.value,
                    "query_attempts": [],
                },
            )
            for variant in variants
            if not _cosmic_identifiers(variant.cosmic_id)
        }
        if not searchable_variants:
            return results
        sync_browser, browser_error, _ = self._browser_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        with sync_browser() as runtime:
            context = self._launch_browser_context(
                runtime,
                "COSMIC",
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                background=self.browser_background,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    self.login_url("COSMIC"),
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                self._ensure_cosmic_grch37(page)
                if not self._try_saved_login("COSMIC", page):
                    results.update({
                        self.variant_key(variant): DatabaseEvidence(
                            "COSMIC",
                            "login_required",
                            "COSMIC sign-in failed or is not configured. Save the "
                            "COSMIC email/password in Settings or use Sign In / Refresh.",
                            accession=variant.cosmic_id,
                            url=page.url,
                        )
                        for variant in searchable_variants
                    })
                    return results
                self._ensure_cosmic_grch37(page)

                for index, variant in enumerate(searchable_variants, start=1):
                    self._check_cancelled()
                    key = self.variant_key(variant)
                    if progress:
                        progress(
                            f"COSMIC browser lookup {index}/{len(searchable_variants)}: "
                            f"{variant.cosmic_id}"
                        )
                    try:
                        results[key] = self._lookup_cosmic_variant(
                            page,
                            variant,
                            artifact_directory,
                            progress=progress,
                        )
                    except Exception as exc:
                        results[key] = DatabaseEvidence(
                            "COSMIC",
                            "error",
                            f"COSMIC browser lookup failed: {exc}",
                            accession=variant.cosmic_id,
                            url=self.query_url("COSMIC", variant),
                        )
                    self._publish_variant_result(variant, results[key])
                    if index < len(searchable_variants):
                        self._wait_between_queries(page, "COSMIC", progress=progress)
            finally:
                try:
                    context.close()
                except browser_error:
                    pass
        return results

    def _lookup_cosmic_variant(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> DatabaseEvidence:
        identifiers = _cosmic_identifiers(variant.cosmic_id)
        if not identifiers:
            return DatabaseEvidence(
                "COSMIC",
                "not_applicable",
                "ingen COSMIC-ID",
                accession=variant.cosmic_id,
                raw={
                    "failure_kind": ProviderFailureKind.NOT_FOUND.value,
                    "query_attempts": [],
                },
            )
        attempts: list[str] = []
        attempt_results: list[dict[str, str]] = []
        last_result: DatabaseEvidence | None = None
        conflicting_result: DatabaseEvidence | None = None
        for cosmic_id in identifiers:
            attempts.append(cosmic_id)
            cache_key = (
                cosmic_id, variant.symbol, variant.hgvsc, variant.hgvsp,
                variant.genomic_location, variant.ref_allele, variant.alt_allele,
            )
            if cache_key in self._cosmic_cache:
                result = deepcopy(self._cosmic_cache[cache_key])
                if progress:
                    progress(f"COSMIC cache hit: {cosmic_id}")
            else:
                candidate = replace(variant, cosmic_id=cosmic_id)
                result = self._lookup_cosmic_with_retry(
                    page,
                    candidate,
                    self.query_url("COSMIC", candidate),
                    artifact_directory,
                    progress=progress,
                )
                if result.status == "found":
                    self._cosmic_cache[cache_key] = deepcopy(result)
            result.raw["query_attempts"] = list(attempts)
            attempt_results.append(
                {"query": cosmic_id, "status": result.status}
            )
            result.raw["query_attempt_results"] = list(attempt_results)
            if result.status == "found":
                return result
            last_result = result
            if result.status == "identity_mismatch":
                conflicting_result = result
            if result.status not in {"not_found", "identity_mismatch"}:
                return result
        if conflicting_result is not None:
            return DatabaseEvidence(
                "COSMIC", "manual_review",
                "COSMIC resolved an identifier to a different variant; none of the "
                "remaining identifiers produced a verified match. Review the source identifiers.",
                accession=conflicting_result.accession, url=conflicting_result.url,
                raw={**conflicting_result.raw,
                    "failure_kind": ProviderFailureKind.IDENTITY_MISMATCH.value,
                    "query_attempts": list(attempts),
                    "query_attempt_results": list(attempt_results)},
            )
        if last_result is not None:
            return last_result
        return DatabaseEvidence(
            "COSMIC",
            "invalid_query",
            "No COSM/COSV identifier was available.",
            raw={"failure_kind": ProviderFailureKind.NOT_FOUND.value},
        )

    def _lookup_cosmic_with_retry(
        self,
        page: Any,
        variant: VariantRecord,
        query_url: str,
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> DatabaseEvidence:
        """Retry one transient COSMIC navigation/render failure without changing identity."""
        for attempt in range(2):
            self._check_cancelled()
            try:
                page.goto(
                    query_url,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                dismiss_known_overlays(page)
                if self._login_required("COSMIC", page.url):
                    return DatabaseEvidence(
                        "COSMIC",
                        "login_required",
                        "COSMIC session expired; use Sign In / Refresh and retry.",
                        accession=variant.cosmic_id,
                        url=page.url,
                    )
                try:
                    self._resolve_cosmic_mutation_page(page, variant)
                    dismiss_known_overlays(page)
                    if self._login_required("COSMIC", page.url):
                        raise ProviderLookupError(ProviderFailureKind.LOGIN_REQUIRED, "COSMIC session expired.")
                    self._wait_for_cosmic_result(page)
                except ProviderLookupError as exc:
                    if exc.kind != ProviderFailureKind.NOT_FOUND:
                        raise
                    return self._lookup_cosmic_migration(
                        page, variant, artifact_directory, progress=progress
                    )
                return self._capture_cosmic_result(
                    variant, page, artifact_directory
                )
            except ProviderLookupError as exc:
                if exc.kind != ProviderFailureKind.TRANSIENT or attempt == 1:
                    diagnostic = {}
                    if exc.kind == ProviderFailureKind.TRANSIENT:
                        # Keep failure evidence local, outside successful capture
                        # records, so a next export can diagnose the actual page.
                        try:
                            diagnostic["visible_text_preview"] = page.locator("body").inner_text()[:12_000]
                        except Exception:
                            pass
                        try:
                            artifact_directory.mkdir(parents=True, exist_ok=True)
                            base = self._screenshot_path(artifact_directory, "COSMIC", variant)
                            path = base.with_name(f"{base.stem}-lookup-timeout.png")
                            page.screenshot(path=str(path), full_page=False)
                            diagnostic["diagnostic_screenshot"] = str(path)
                        except Exception:
                            diagnostic["diagnostic_capture_error"] = "COSMIC diagnostic screenshot was unavailable."
                    return DatabaseEvidence(
                        "COSMIC",
                        exc.kind.value,
                        str(exc),
                        accession=_cosmic_identifier(variant.cosmic_id),
                        url=page.url,
                        raw={"failure_kind": exc.kind.value, **exc.diagnostics, **diagnostic},
                    )
            except Exception as exc:
                if attempt == 1 or not _transient_browser_failure(exc):
                    raise
            if progress:
                progress(
                    f"COSMIC: transient page failure for {variant.cosmic_id}; "
                    "retrying once"
                )
            self._interruptible_page_wait(page, 750)
        raise RuntimeError("COSMIC retry loop ended unexpectedly.")

    def _lookup_cosmic_migration(
        self, page: Any, variant: VariantRecord, artifact_directory: Path,
        *, progress: Callable[[str], None] | None,
    ) -> DatabaseEvidence:
        """Use GRCh38 only to discover an explicit legacy-ID replacement."""
        original = _cosmic_identifier(variant.cosmic_id)
        if progress:
            progress(f"COSMIC: {original} not found in GRCh37; checking GRCh38 for an ID migration")
        try:
            self._ensure_cosmic_genome(page, "38")
            page.goto(
                self.query_url("COSMIC", variant) + "&genome=38",
                wait_until="domcontentloaded", timeout=self.navigation_timeout_ms,
            )
            if self._login_required("COSMIC", page.url):
                raise ProviderLookupError(ProviderFailureKind.LOGIN_REQUIRED, "COSMIC session expired.")
            self._open_cosmic_mutation_page(page, variant)
            notice = page.locator("body").inner_text(timeout=self.navigation_timeout_ms)
            replacement = _cosmic_migration_target(notice, original)
            migration_url = page.url
        finally:
            # Never leave the provider lane in GRCh38, even on rejection/cancellation.
            if not self._login_required("COSMIC", page.url):
                self._ensure_cosmic_grch37(page)
        candidate = replace(variant, cosmic_id=replacement)
        page.goto(
            self.query_url("COSMIC", candidate),
            wait_until="domcontentloaded", timeout=self.navigation_timeout_ms,
        )
        if self._login_required("COSMIC", page.url):
            raise ProviderLookupError(ProviderFailureKind.LOGIN_REQUIRED, "COSMIC session expired.")
        self._resolve_cosmic_mutation_page(page, candidate)
        self._verify_cosmic_grch37(page)
        self._wait_for_cosmic_result(page)
        evidence = self._capture_cosmic_result(
            candidate, page, artifact_directory, require_variant_identity=True,
        )
        evidence.raw.update({
            "original_cosmic_id": original,
            "resolved_cosmic_id": replacement,
            "migration_source_url": migration_url,
            "migration_notice": notice[:12_000],
            "fallback_route": "GRCh37 -> GRCh38 ID migration -> GRCh37",
        })
        if evidence.status == "found":
            evidence.raw.update({"cosmic_id_changed": True, "assembly_verified": "GRCh37"})
            evidence.summary = f"endret cosmic id: {original} → {replacement}. {evidence.summary}"
        self._write_audit(evidence, self._screenshot_path(artifact_directory, "COSMIC", variant).with_suffix(".audit.json"))
        return evidence

    def _open_cosmic_mutation_page(
        self, page: Any, variant: VariantRecord
    ) -> None:
        """Resolve an exact legacy identifier within the selected assembly."""
        cosmic_number = _cosmic_numeric_id(variant.cosmic_id)
        if not cosmic_number:
            raise ValueError("COSMIC identifier is missing its numeric component.")
        if self._login_required("COSMIC", page.url):
            raise ProviderLookupError(
                ProviderFailureKind.LOGIN_REQUIRED,
                "COSMIC session expired; use Sign In / Refresh and retry.",
            )
        if "/cosmic/mutation/overview" not in page.url:
            links = page.locator("a[href*='/cosmic/mutation/overview']")
            attempts = max(1, self.navigation_timeout_ms // 500)
            for _ in range(attempts):
                self._check_cancelled()
                candidates = [
                    href
                    for href in links.evaluate_all("nodes => nodes.map(node => node.href)")
                    if href and parse_qs(urlsplit(href).query).get("merge") == [cosmic_number]
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
                    raise ProviderLookupError(
                        ProviderFailureKind.AMBIGUOUS,
                        f"COSMIC returned multiple canonical pages for {variant.cosmic_id}."
                    )
                self._raise_for_cosmic_terminal_page(page)
                page.wait_for_timeout(500)
            else:
                raise ProviderLookupError(
                    ProviderFailureKind.TRANSIENT,
                    f"COSMIC did not finish rendering a canonical mutation link for {variant.cosmic_id}.",
                    diagnostics={
                        "failure_stage": "canonical_link_resolution",
                        "render_diagnostics": {"page_type": "search", "result_links": 0},
                    },
                )

    def _resolve_cosmic_mutation_page(
        self, page: Any, variant: VariantRecord
    ) -> None:
        """Resolve a legacy identifier and switch the canonical page to GRCh37."""
        self._open_cosmic_mutation_page(page, variant)
        grch37_links = page.locator("a[href*='genome=37'][href*='id=']")
        candidates: list[str] = []
        for _ in range(max(1, self.navigation_timeout_ms // 500)):
            self._check_cancelled()
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
            self._raise_for_cosmic_terminal_page(page)
            page.wait_for_timeout(500)
        if not candidates:
            raise ProviderLookupError(
                ProviderFailureKind.TRANSIENT,
                "COSMIC did not finish rendering a GRCh37 mutation link.",
                diagnostics={
                    "failure_stage": "assembly_link_resolution",
                    "render_diagnostics": {"page_type": "record", "result_links": 0},
                },
            )
        if len(candidates) > 1:
            raise ProviderLookupError(
                ProviderFailureKind.AMBIGUOUS,
                "COSMIC exposed multiple GRCh37 mutation links.",
            )
        page.goto(
            candidates[0],
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )
        self._verify_cosmic_grch37(page)

    def _raise_for_cosmic_terminal_page(self, page: Any) -> None:
        if self._login_required("COSMIC", page.url):
            raise ProviderLookupError(
                ProviderFailureKind.LOGIN_REQUIRED,
                "COSMIC session expired; use Sign In / Refresh and retry.",
            )
        body_text = page.locator("body").inner_text()
        if re.search(r"\b(?:mutation not found|no results found|no results were found)\b", body_text, re.I):
            raise ProviderLookupError(
                ProviderFailureKind.NOT_FOUND,
                "COSMIC explicitly reported no matching mutation.",
            )

    def _cosmic_grch37_options(self, page: Any) -> list[dict[str, str]]:
        options = page.locator("a[href*='genome=37']").evaluate_all(
            "nodes => nodes.map(node => ({href: node.href, text: node.textContent.trim()}))"
        )
        return [
            option
            for option in options
            if isinstance(option, dict)
            and re.match(r"^GRCh37\b", option.get("text", ""), re.IGNORECASE)
        ]

    def _ensure_cosmic_grch37(self, page: Any) -> None:
        """Select GRCh37 through COSMIC's global Genome Version menu."""
        options = self._cosmic_grch37_options(page)
        if any("✔" in option.get("text", "") or "✓" in option.get("text", "") for option in options):
            return
        targets = list(
            dict.fromkeys(option.get("href", "") for option in options if option.get("href"))
        )
        if len(targets) != 1:
            raise RuntimeError("COSMIC GRCh37 genome menu option was not uniquely available.")
        page.goto(
            targets[0],
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )
        page.wait_for_timeout(250)
        self._verify_cosmic_grch37(page)

    def _ensure_cosmic_genome(self, page: Any, genome: str) -> None:
        options = page.locator(f"a[href*='genome={genome}']").evaluate_all(
            "nodes => nodes.map(node => ({href: node.href, text: node.textContent.trim()}))"
        )
        options = [option for option in options if isinstance(option, dict)
                   and re.match(rf"^GRCh{genome}\b", option.get("text", ""), re.I)]
        if any("✔" in option["text"] or "✓" in option["text"] for option in options):
            return
        targets = list(dict.fromkeys(option["href"] for option in options if option.get("href")))
        if len(targets) != 1:
            raise ProviderLookupError(ProviderFailureKind.LAYOUT_CHANGED, f"COSMIC GRCh{genome} menu was not unique.")
        page.goto(targets[0], wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)

    def _verify_cosmic_grch37(self, page: Any) -> None:
        options = self._cosmic_grch37_options(page)
        selected = any(
            ("✔" in option.get("text", "") or "✓" in option.get("text", ""))
            for option in options
        )
        genome = re.search(r"[?&]genome=([^&#]*)", page.url, re.IGNORECASE)
        if not selected or (genome is not None and genome.group(1) != "37"):
            raise RuntimeError("COSMIC GRCh37 was not selected; result capture was stopped.")

    def _search_clinvar(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Resolve and verify ClinVar entirely through its public website."""
        sync_browser, browser_error, _ = self._browser_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        results: dict[str, DatabaseEvidence] = {}
        with sync_browser() as runtime:
            context = self._launch_browser_context(
                runtime,
                "ClinVar",
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                background=self.browser_background,
            )
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
                    try:
                        results[key] = self._lookup_clinvar_variant(
                            page, variant, artifact_directory
                        )
                    except Exception as exc:
                        results[key] = DatabaseEvidence(
                            database="ClinVar",
                            status="error",
                            summary=f"ClinVar website lookup failed: {exc}",
                            accession=_review_query(variant),
                            url=page.url,
                            raw={"failure_stage": "website lookup"},
                        )
                    self._publish_variant_result(variant, results[key])
                    if index < len(variants):
                        self._wait_between_queries(page, "ClinVar", progress=progress)
            finally:
                try:
                    context.close()
                except browser_error:
                    pass
        return results

    def _lookup_clinvar_variant(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
    ) -> DatabaseEvidence:
        attempts: list[str] = []
        rejected_records: set[str] = set()
        ambiguous_url = ""
        for query in _clinvar_queries(variant):
            protein_fallback = query == _clinvar_protein_query(variant)
            attempts.append(query)
            query_url = (
                "https://www.ncbi.nlm.nih.gov/clinvar/?term="
                f"{quote(query, safe='')}"
            )
            page.goto(
                query_url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            try:
                body_text, links = self._wait_for_clinvar_result(page)
            except TimeoutError as exc:
                return DatabaseEvidence(
                    "ClinVar", "error", str(exc),
                    accession=_review_query(variant), url=page.url,
                    raw={
                        "failure_kind": ProviderFailureKind.TRANSIENT.value,
                        "failure_stage": "result_rendering",
                        **getattr(exc, "diagnostics", {}),
                        "query_attempts": list(attempts),
                    },
                )
            if "/clinvar/variation/" not in page.url:
                candidates = _matching_clinvar_links(
                    links, variant, allow_protein=protein_fallback
                )
                candidates = [
                    candidate for candidate in candidates
                    if urlsplit(candidate).path.rstrip("/") not in rejected_records
                ]
                if len(candidates) > 1:
                    # Protein searches can legitimately match multiple nucleotide
                    # variants. Exhaust more precise queries before asking the user.
                    ambiguous_url = page.url
                    continue
                if not candidates:
                    continue
                page.goto(
                    candidates[0],
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                try:
                    body_text, _ = self._wait_for_clinvar_result(page)
                except TimeoutError as exc:
                    return DatabaseEvidence(
                        "ClinVar", "error", str(exc),
                        accession=_review_query(variant), url=page.url,
                        raw={
                            "failure_kind": ProviderFailureKind.TRANSIENT.value,
                            "failure_stage": "result_rendering",
                            **getattr(exc, "diagnostics", {}),
                            "query_attempts": list(attempts),
                        },
                    )
            verification = _clinvar_identity(
                body_text, variant, allow_protein=protein_fallback
            )
            if not verification.accepted:
                rejected_records.add(urlsplit(page.url).path.rstrip("/"))
                continue
            accession_match = re.search(
                r"Accession:\s*(VCV\d+(?:\.\d+)?)", body_text, re.IGNORECASE
            )
            evidence = DatabaseEvidence(
                "ClinVar",
                "found",
                "ClinVar website result verified against the variant and GRCh37 location.",
                accession=accession_match.group(1) if accession_match else "",
                url=page.url,
                raw={
                    "assembly_verified": "GRCh37",
                    "identity_verification": asdict(verification),
                    "query_attempts": list(attempts),
                },
            )
            captured = self._capture_clinvar_result(
                variant, evidence, page, artifact_directory
            )
            captured.raw["query_attempts"] = list(attempts)
            return captured
        if ambiguous_url:
            return DatabaseEvidence(
                "ClinVar", "manual_review",
                "ClinVar returned multiple exact-looking rows and the precise "
                "website fallbacks did not resolve the ambiguity; manual selection is required.",
                accession=_review_query(variant), url=ambiguous_url,
                raw={"query_attempts": list(attempts)},
            )
        return DatabaseEvidence(
            "ClinVar",
            "not_found",
            "No exact ClinVar website result matched the requested variant on GRCh37.",
            accession=_review_query(variant),
            url=page.url,
            raw={"query_attempts": attempts},
        )

    def _wait_for_clinvar_result(
        self, page: Any
    ) -> tuple[str, list[dict[str, str]]]:
        """Read a completed search or record before deciding identity or absence."""
        for poll in range(max(1, self.navigation_timeout_ms // 250) + 1):
            self._check_cancelled()
            body_text = page.locator("body").inner_text(
                timeout=self.navigation_timeout_ms
            )
            diagnostics: dict[str, object] = {
                "page_type": "record" if "/clinvar/variation/" in page.url else "search",
                "record_metadata": bool(re.search(r"\b(?:Variation ID|VCV\d+)", body_text, re.I)),
                "hgvs_title": False,
                "genomic_location": bool(_clinvar_locations(body_text)),
                "result_links": 0,
            }
            if diagnostics["page_type"] == "record":
                # Readiness is separate from acceptance: a complete GRCh38 or
                # differently sized record must reach the strict identity check.
                diagnostics["hgvs_title"] = bool(re.search(
                    r"(?m)^[ \t]*(?:[A-Za-z][\w.-]*[ \t]*"
                    r"\([A-Za-z][\w.-]*\)[ \t]*:[ \t]*|"
                    r"(?!(?:HGVS|Name|Gene|Location|Variation|Accession)\b)"
                    r"[A-Za-z][A-Za-z0-9-]*(?:[ \t]*:[ \t]*|[ \t]+))"
                    r"[cnpg]\.\S+",
                    body_text, re.I,
                ))
                if all(diagnostics[field] for field in (
                    "record_metadata", "genomic_location", "hgvs_title"
                )):
                    return body_text, []
            else:
                links = page.locator("a[href*='/clinvar/variation/']").evaluate_all(
                    "nodes => nodes.map(node => ({"
                    "text: (node.closest('tr, article, li, .rprt')?.innerText || "
                    "node.parentElement?.innerText || node.innerText).trim(), "
                    "href: node.href}))"
                )
                diagnostics["result_links"] = len(links)
                if any(
                    isinstance(item, dict)
                    and item.get("href")
                    and re.search(r"\b[cnpg]\.", str(item.get("text") or ""), re.I)
                    for item in links
                ):
                    return body_text, links
                if re.search(
                    r"\b(?:no (?:items|results)(?: were)? found|0 results|"
                    r"your search did not find|search results:\s*0\s+variants?\b)", body_text, re.I,
                ):
                    return body_text, []
            if poll < max(1, self.navigation_timeout_ms // 250):
                self._interruptible_page_wait(page, 250)
        raise _ClinVarRenderingTimeout(diagnostics)

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
        attempts = max(1, self.navigation_timeout_ms // 500)
        for _ in range(attempts):
            try:
                body_text = page.locator("body").inner_text()
            except (AttributeError, TypeError):
                body_text = ""
            if "mutation not found" in body_text.casefold():
                detail = re.search(
                    r"The mutation with ID\s+\d+\s+was not found in our database\.",
                    body_text,
                    flags=re.IGNORECASE,
                )
                raise ProviderLookupError(
                    ProviderFailureKind.NOT_FOUND,
                    detail.group(0) if detail else "COSMIC mutation was not found.",
                )
            try:
                self._cosmic_section(page, "Overview")
                self._cosmic_section(page, "Tissue distribution")
                rows = self._cosmic_section(page, "Samples").locator("tbody tr")
                if rows.count() > 0:
                    return
            except RuntimeError:
                pass
            page.wait_for_timeout(500)
        raise TimeoutError("COSMIC sample table did not finish loading.")

    def _capture_cosmic_result(
        self,
        variant: VariantRecord,
        page: Any,
        artifact_directory: Path,
        *, require_variant_identity: bool = False,
    ) -> DatabaseEvidence:
        body_text = page.locator("body").inner_text(timeout=self.navigation_timeout_ms)
        cosmic_id = _cosmic_identifier(variant.cosmic_id)
        original_cosmic_id = cosmic_id
        try:
            cosmic_id = _cosmic_migration_target(body_text, original_cosmic_id)
            require_variant_identity = True
        except ProviderLookupError as exc:
            if exc.kind != ProviderFailureKind.NOT_FOUND:
                raise
        cdna = _cdna_change(variant.hgvsc)
        protein = _protein_change(variant.hgvsp)
        identifier_matches = cosmic_id in _cosmic_identifiers(body_text)
        variant_identity_matches = bool(
            variant.symbol
            and re.search(
                r"(?<![A-Za-z0-9_-])" + re.escape(variant.symbol)
                + r"(?![A-Za-z0-9_-])", body_text, re.I,
            )
            and any(
                value not in {"", "?", "=", "-", "."}
                and _contains_hgvs_change(body_text, value)
                for value in (cdna, protein)
            )
        )
        if (require_variant_identity and not variant_identity_matches) or (
            cosmic_id and not identifier_matches and not variant_identity_matches
        ):
            return DatabaseEvidence(
                "COSMIC",
                "identity_mismatch",
                f"COSMIC resolved {cosmic_id}, but the page did not match the requested variant.",
                accession=cosmic_id,
                url=page.url,
                raw={"failure_kind": ProviderFailureKind.IDENTITY_MISMATCH.value},
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
        if cosmic_id != original_cosmic_id:
            self._verify_cosmic_grch37(page)
            evidence.raw.update({
                "cosmic_id_changed": True,
                "original_cosmic_id": original_cosmic_id,
                "resolved_cosmic_id": cosmic_id,
                "assembly_verified": "GRCh37",
                "migration_source_url": page.url,
            })
            evidence.summary = f"endret cosmic id: {original_cosmic_id} → {cosmic_id}. {evidence.summary}"
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
                context = self._launch_browser_context(
                    runtime,
                    "Franklin",
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 1000},
                    background=self.browser_background,
                )
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

                pending = list(variants)
                retryable_statuses = {"timeout", "error"}
                for pass_index in range(1, self.franklin_attempts + 1):
                    retry_variants: list[VariantRecord] = []
                    for index, variant in enumerate(pending, start=1):
                        self._check_cancelled()
                        key = self.variant_key(variant)
                        if progress:
                            pass_label = (
                                "Franklin browser retry"
                                if pass_index > 1
                                else "Franklin browser lookup"
                            )
                            progress(
                                f"{pass_label} {index}/{len(pending)}: "
                                f"{_franklin_search_query(variant)}"
                            )
                        evidence = self._resolve_franklin_queries(
                            page,
                            variant,
                            artifact_directory,
                            progress=progress,
                        )
                        results[key] = evidence
                        self._publish_variant_result(variant, evidence)
                        if (
                            evidence.status in retryable_statuses
                            and str(evidence.raw.get("failure_kind") or "")
                            in {"", ProviderFailureKind.TRANSIENT.value}
                        ):
                            retry_variants.append(variant)
                        if index < len(pending):
                            self._wait_between_queries(
                                page, "Franklin", progress=progress
                            )
                    pending = retry_variants
                    if not pending:
                        break
                    if pass_index < self.franklin_attempts:
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
        query_timings: list[dict[str, str | float]] = []
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
            query_started_at = time.monotonic()
            evidence = self._search_franklin_query(
                page,
                variant,
                query,
                artifact_directory,
                progress=progress,
            )
            query_elapsed = time.monotonic() - query_started_at
            query_timings.append(
                {
                    "query": query,
                    "status": evidence.status,
                    "duration_seconds": round(query_elapsed, 3),
                }
            )
            if progress:
                progress(
                    f"Franklin: {evidence.status} completed in "
                    f"{query_elapsed:.1f}s"
                )
            if evidence.status == "found":
                break
            if str(evidence.raw.get("failure_kind") or "") in {
                ProviderFailureKind.LAYOUT_CHANGED.value,
                ProviderFailureKind.AMBIGUOUS.value,
                ProviderFailureKind.LOGIN_REQUIRED.value,
            }:
                break
            if evidence.status not in {
                "identity_mismatch",
                "timeout",
                "not_found",
                "error",
            }:
                break
        evidence.raw["query_attempts"] = list(query_attempts)
        evidence.raw["query_timings"] = query_timings
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
        failure_kind = ProviderFailureKind.TRANSIENT
        stage = "opening the Franklin search page"
        try:
            page.goto(
                FRANKLIN_HOME_URL,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            dismiss_known_overlays(page)
            stage = "waiting for the Franklin search input"
            dismiss_franklin_onboarding(page)
            search = page.locator(
                "input[placeholder='Enter variant, gene or select an example above']"
            )
            search.wait_for(state="visible", timeout=self.navigation_timeout_ms)
            stage = "selecting the hg19 somatic search mode"
            self._select_franklin_search_mode(page)
            stage = "submitting the Franklin query"
            search.fill(query)
            search.press("Enter")
            dismissed = dismiss_franklin_onboarding(page)
            if dismissed and progress:
                progress("Franklin: skipped optional first-use material dialog")
            stage = "resolving the Franklin variant route"
            self._open_franklin_resolved_variant(page, variant)
            stage = "waiting for the Franklin classification"
            for _ in range(60):
                self._check_cancelled()
                dismiss_franklin_onboarding(page)
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
                    assessment = self._capture_franklin_assessment_with_retry(
                        page, variant, artifact_directory
                    )
                    evidence.raw.setdefault("screenshots", []).extend(assessment)
                except Exception as exc:
                    evidence.raw["screenshot_warning"] = (
                        "Franklin assessment-tools screenshot could not be captured: "
                        f"{exc}"
                    )
                    evidence.status = "partial_capture"
                    evidence.raw["failure_stage"] = "assessment_capture"
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
            status = "error"
            error = (
                "Franklin returned 'Something went wrong'."
                if "Something went wrong" in last_text
                else "Franklin classification did not finish rendering within 60 seconds."
            )
        except ProviderLookupError as exc:
            status = exc.kind.value
            failure_kind = exc.kind
            error = f"Franklin failed while {stage}: {exc}"
        except browser_timeout:
            status = "timeout"
            error = f"Franklin timed out while {stage}."
        except Exception as exc:
            status = "error"
            failure_kind = (
                ProviderFailureKind.TRANSIENT if _transient_browser_failure(exc)
                else ProviderFailureKind.LAYOUT_CHANGED
            )
            error = f"Franklin failed while {stage}: {exc}"
        return DatabaseEvidence(
            "Franklin",
            status,
            error or "Franklin did not return a classification.",
            accession=query,
            url=page.url,
            raw={
                "failure_kind": failure_kind.value,
                "failure_stage": stage,
                "visible_text_preview": last_text[:12_000],
            },
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
        question_actions = 0
        for _ in range(40):
            self._check_cancelled()
            dismiss_franklin_onboarding(page)
            if re.search(r"/clinical-db/variant/snp(?:Tumor)?/", page.url):
                return
            if self._advance_franklin_case_questions(page):
                question_actions += 1
                if question_actions == 4:
                    break
                page.wait_for_timeout(250)
                continue
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
                raise ProviderLookupError(
                    ProviderFailureKind.AMBIGUOUS,
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

    @staticmethod
    def _advance_franklin_case_questions(page: Any) -> bool:
        """Skip optional first-visit case details without inventing clinical data."""
        if "/clinical-db/home" not in page.url:
            return False
        text = " ".join(page.locator("body").inner_text().split())
        if "Add your case details" not in text:
            return False
        skip = page.get_by_text("Skip question", exact=True)
        if skip.count() == 1 and skip.is_visible():
            skip.click()
            return True
        if "What is the patient sex?" in text:
            unknown = page.get_by_text("Unknown", exact=True)
            if unknown.count() == 1 and unknown.is_visible():
                unknown.click()
                search = page.get_by_role("button", name="Search", exact=True)
                search.wait_for(state="visible", timeout=2_000)
                if search.count() != 1:
                    raise RuntimeError("Franklin case-details Search button was not unique.")
                search.click()
                return True
        raise RuntimeError("Franklin case questions were visible but no safe skip action was available.")

    def _select_franklin_search_mode(self, page: Any) -> None:
        """Choose the explicitly requested GRCh37/hg19 somatic search mode."""
        comboboxes = page.get_by_role("combobox")
        for _ in range(max(1, self.navigation_timeout_ms // 250)):
            self._check_cancelled()
            if comboboxes.count() >= 2:
                break
            self._interruptible_page_wait(page, 250)
        if comboboxes.count() < 2:
            raise ProviderLookupError(
                ProviderFailureKind.LAYOUT_CHANGED,
                "Franklin reference/type selectors were not available.",
            )
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
                raise ProviderLookupError(
                    ProviderFailureKind.LAYOUT_CHANGED,
                    f"Franklin search option was not uniquely available: {option_name}"
                )
            option_click = getattr(option, "click_physical", option.click)
            option_click()
            page.wait_for_timeout(150)

    def _publish_mtbp_results(self, results: dict[str, DatabaseEvidence]) -> None:
        """Publish parsed results before image capture and disk writes finish.

        Each provider lane is serial. Snapshot objects must be independent of
        later capture/cleanup mutations delivered through Qt's queued signals.
        """
        if self._result_checkpoint is None:
            return
        snapshot = {}
        for key, evidence in results.items():
            item = deepcopy(evidence)
            if item.status == "found":
                item.status = "partial_capture"
            snapshot[key] = [item]
        self._result_checkpoint(snapshot)

    def _search_mtbp(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
        prior_evidence: dict[str, list[DatabaseEvidence]] | None = None,
    ) -> dict[str, DatabaseEvidence]:
        """Run independent MTBP reports so each screenshot belongs to one variant."""
        results: dict[str, DatabaseEvidence] = {}
        retained: list[tuple[VariantRecord, DatabaseEvidence]] = []
        for variant in variants:
            key = self.variant_key(variant)
            prior = next(
                (
                    item
                    for item in (prior_evidence or {}).get(key, [])
                    if item.database == "MTBP"
                ),
                None,
            )
            if prior is None:
                continue
            cleanup = prior.raw.get("remote_report_cleanup")
            cleanup_status = cleanup.get("status") if isinstance(cleanup, dict) else ""
            analysis_id = str(prior.raw.get("analysis_id") or "")
            if analysis_id.startswith("ARCHER-") and (
                prior.status in {"timeout", "partial_capture", "submission_unknown"}
                or (
                    prior.status == "found"
                    and cleanup_status not in {"deleted", "already_absent"}
                )
            ):
                retained.append((variant, prior))
        if retained:
            if progress:
                progress(
                    f"MTBP: recovering {len(retained)} retained report(s) "
                    "before submission"
                )
            try:
                recovered = self._recover_mtbp_timeouts(
                    retained,
                    artifact_directory,
                    progress=progress,
                )
            except Exception as exc:
                recovered = {}
                if progress:
                    progress(
                        "MTBP: retained-report recovery could not start; keeping "
                        f"the saved report IDs for retry ({exc})"
                    )
            confirmed_absent = {
                key
                for key, evidence in recovered.items()
                if isinstance(evidence.raw.get("remote_report_recovery"), dict)
                and evidence.raw["remote_report_recovery"].get("status")
                == "confirmed_absent"
            }
            results.update(
                {
                    key: evidence
                    for key, evidence in recovered.items()
                    if key not in confirmed_absent
                }
            )
            for variant, prior in retained:
                key = self.variant_key(variant)
                if key not in confirmed_absent:
                    results.setdefault(key, prior)
        pending_variants = [
            variant
            for variant in variants
            if self.variant_key(variant) not in results
        ]
        if pending_variants:
            self._check_cancelled()
            if progress:
                progress(
                    "MTBP: submitting one combined patient report with "
                    f"{len(pending_variants)} variant(s)"
                )
            protected_analysis_ids = {
                str(evidence.raw.get("analysis_id") or "")
                for evidence in results.values()
                if str(evidence.raw.get("analysis_id") or "").startswith("ARCHER-")
            }
            batch_kwargs: dict[str, Any] = {"progress": progress}
            if protected_analysis_ids:
                batch_kwargs["protected_analysis_ids"] = protected_analysis_ids
            current = self._search_mtbp_batch(
                pending_variants, artifact_directory, **batch_kwargs
            )
            for evidence in current.values():
                evidence.url = ""
                for record in evidence.raw.get("screenshots", []):
                    if isinstance(record, dict):
                        record["url"] = ""
            results.update(current)
        pending = [
            (variant, results[self.variant_key(variant)])
            for variant in variants
            if self.variant_key(variant) in results
            and results[self.variant_key(variant)].status
            in {"timeout", "submission_unknown"}
            and results[self.variant_key(variant)].raw.get("analysis_id")
        ]
        if pending:
            if progress:
                progress(
                    f"MTBP: rechecking {len(pending)} late report(s) without resubmitting"
                )
            try:
                recovered = self._recover_mtbp_timeouts(
                    pending,
                    artifact_directory,
                    progress=progress,
                )
            except Exception as exc:
                recovered = {}
                if progress:
                    progress(
                        "MTBP: late-report recovery could not run; keeping the "
                        f"retryable timeout ({exc})"
                    )
            for evidence in recovered.values():
                evidence.url = ""
                for record in evidence.raw.get("screenshots", []):
                    if isinstance(record, dict):
                        record["url"] = ""
            results.update(recovered)
        return results

    def _recover_mtbp_timeouts(
        self,
        pending: list[tuple[VariantRecord, DatabaseEvidence]],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Capture reports that appeared after their deadline without a new submission."""
        sync_browser, browser_error, browser_timeout = self._browser_api()
        recovered: dict[str, DatabaseEvidence] = {}
        report_captures: dict[str, Path | None] = {}
        report_pages: dict[str, tuple[str, list[dict[str, Any]], str]] = {}
        report_audits: dict[str, list[tuple[Path, VariantRecord, DatabaseEvidence]]] = {}
        with sync_browser() as runtime:
            context = self._launch_browser_context(
                runtime,
                "MTBP",
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                background=self.browser_background,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._goto_with_retries(page, self.login_url("MTBP"))
                if not self._session_authenticated("MTBP", page):
                    self._try_saved_login("MTBP", page)
                if not self._session_authenticated("MTBP", page):
                    return recovered
                for variant, timed_out in pending:
                    self._check_cancelled()
                    analysis_id = str(timed_out.raw.get("analysis_id") or "")
                    if timed_out.status == "found":
                        if any(str(item.raw.get("analysis_id") or "") == analysis_id
                               and item.status != "found" for _, item in pending):
                            report_audits.setdefault(analysis_id, []).append((
                                self._screenshot_path(artifact_directory, "MTBP", variant).with_suffix(".audit.json"),
                                variant,
                                timed_out,
                            ))
                            recovered[self.variant_key(variant)] = timed_out
                            continue
                    if analysis_id in report_pages:
                        report_count = 1
                        report_link = None
                    else:
                        self._goto_with_retries(page, MTBP_REPORTS_URL, attempts=2)
                        report_link = page.get_by_role(
                            "link", name=analysis_id, exact=True
                        )
                        report_count = report_link.count()
                    if timed_out.status == "found":
                        audit_path = self._screenshot_path(
                            artifact_directory, "MTBP", variant
                        ).with_suffix(".audit.json")
                        self._persist_mtbp_report_audit(audit_path, variant, timed_out)
                        if report_count == 0:
                            cleanup = {
                                "status": "already_absent",
                                "message": (
                                    "The exact MTBP report was already absent from "
                                    "the portal."
                                ),
                            }
                        elif report_count == 1:
                            cleanup = self._delete_mtbp_report(page, analysis_id)
                        else:
                            cleanup = {
                                "status": "failed",
                                "message": (
                                    "The exact MTBP analysis ID was not unique in "
                                    "the report list."
                                ),
                            }
                        timed_out.raw["remote_report_cleanup"] = cleanup
                        self._persist_mtbp_report_audit(audit_path, variant, timed_out)
                        recovered[self.variant_key(variant)] = timed_out
                        continue
                    if report_count != 1:
                        if progress:
                            progress(f"MTBP: late report is still pending ({analysis_id})")
                        if timed_out.status in {"timeout", "submission_unknown"}:
                            recovered[self.variant_key(variant)] = timed_out
                        elif timed_out.status == "partial_capture":
                            if report_count == 0:
                                timed_out.raw["remote_report_recovery"] = {
                                    "status": "confirmed_absent"
                                }
                            recovered[self.variant_key(variant)] = timed_out
                        continue
                    if analysis_id in report_pages:
                        body_text, report_rows, report_url = report_pages[analysis_id]
                    else:
                        if progress:
                            matching_variants = sum(
                                str(item.raw.get("analysis_id") or "") == analysis_id
                                for _, item in pending
                            )
                            progress(
                                f"MTBP: opening retained report {analysis_id} for "
                                f"{matching_variants} variant(s)"
                            )
                        assert report_link is not None
                        report_link.click()
                        page.wait_for_url(
                            re.compile(r"https://mtbp\.org/patients/.+/report/\d+/?"),
                            timeout=self.navigation_timeout_ms,
                        )
                        body_text = page.locator("body").inner_text(
                            timeout=self.navigation_timeout_ms
                        )
                        version_tooltip = page.locator("[data-tooltip-html*='VEP:']")
                        if version_tooltip.count():
                            version_html = (
                                version_tooltip.first.get_attribute("data-tooltip-html")
                                or ""
                            )
                            body_text += "\n" + re.sub(
                                r"<br\s*/?>", "\n", version_html, flags=re.IGNORECASE
                            )
                        report_rows = self._extract_mtbp_rows(page)
                        report_url = page.url
                        report_pages[analysis_id] = (
                            body_text,
                            report_rows,
                            report_url,
                        )
                    evidence = parse_mtbp_report(
                        body_text,
                        report_rows,
                        [variant],
                        report_url,
                        cancer_type=self.mtbp_cancer_type,
                        complete_submission=False,
                    )[self.variant_key(variant)]
                    evidence.raw["analysis_id"] = analysis_id
                    self._publish_mtbp_results({self.variant_key(variant): evidence})
                    evidence.accession = timed_out.accession
                    screenshot_path = None
                    if analysis_id not in report_captures:
                        try:
                            report_captures[analysis_id] = self._capture_mtbp_full_report(page, artifact_directory, analysis_id)
                        except Exception:
                            report_captures[analysis_id] = None
                    full_report_path = report_captures[analysis_id]
                    if evidence.status == "found" or (
                        full_report_path is not None and evidence.raw.get("gene_candidate_count", 0) > 0
                    ):
                        try:
                            screenshot_path = self._capture_mtbp_variant_with_fallback(
                                page, variant, artifact_directory, full_report_path
                            )
                        except (IncompleteCaptureError, OSError) as exc:
                            if evidence.status == "found":
                                evidence.status = "partial_capture"
                            evidence.summary = (
                                f"{evidence.summary} MTBP screenshot requires retry."
                            ).strip()
                            evidence.raw["capture_validation"] = {
                                "valid": False,
                                "reason": exc.validation.reason if isinstance(exc, IncompleteCaptureError) else str(exc),
                            }
                    screenshots = (
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
                            "submitted_query": timed_out.raw.get(
                                "submitted_query", timed_out.accession
                            ),
                            "query_attempts": timed_out.raw.get("query_attempts", []),
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "screenshot": str(screenshot_path or ""),
                            "screenshots": screenshots,
                            "visible_text_preview": body_text[:12_000],
                            "late_report_recovered": True,
                            "patient_report_screenshot": str(full_report_path or ""),
                        }
                    )
                    report_audits.setdefault(analysis_id, []).append((
                        self._screenshot_path(artifact_directory, "MTBP", variant).with_suffix(".audit.json"),
                        variant,
                        evidence,
                    ))
                    recovered[self.variant_key(variant)] = evidence
                    if progress:
                        progress(
                            f"MTBP: recovered variant {variant.hgvsc or variant.hgvsp} "
                            f"from retained report ({analysis_id})"
                        )
                # A patient report is shared: never delete it after capturing
                # just the first variant in a resumed batch.
                for analysis_id, audit_records in report_audits.items():
                    if len(audit_records) != sum(
                        str(item.raw.get("analysis_id") or "") == analysis_id
                        for _, item in pending
                    ):
                        continue
                    self._finalize_mtbp_report(page, analysis_id, audit_records)
            except browser_timeout:
                return recovered
            finally:
                try:
                    context.close()
                except browser_error:
                    pass
        return recovered

    def _search_mtbp_batch(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
        protected_analysis_ids: set[str] | None = None,
    ) -> dict[str, DatabaseEvidence]:
        """Submit one pseudonymous MTBP analysis and parse its report."""
        results: dict[str, DatabaseEvidence] = {}
        parsed: dict[str, DatabaseEvidence] = {}
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
        query_attempts = {
            self.variant_key(variant): list(
                initial_query_attempts[self.variant_key(variant)]
            )
            for variant, _ in query_pairs
        }
        failure_stage = "browser_startup"
        submission_clicked = False
        context = None
        try:
            with sync_browser() as runtime:
                context = self._launch_browser_context(
                    runtime,
                    "MTBP",
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 1000},
                    background=self.browser_background,
                )
                page = context.pages[0] if context.pages else context.new_page()
                if progress:
                    progress(f"MTBP: submitting {len(submitted_queries)} pseudonymous variants")
                    if learned_fallback_keys:
                        progress(
                            "MTBP: using previously validated GRCh37 fallback for "
                            f"{len(learned_fallback_keys)} variant(s)"
                        )
                failure_stage = "login_navigation"
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

                failure_stage = "report_capacity_preflight"
                preflight_cleanup = self._cleanup_stale_mtbp_reports(
                    page,
                    progress=progress,
                    protected_analysis_ids=protected_analysis_ids,
                )
                self._goto_with_retries(page, self.login_url("MTBP"))
                active_pairs = list(query_pairs)
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
                    analysis_id = run_analysis_id
                    failure_stage = "submission_acceptance"
                    run_button = page.locator("#run-analysis")
                    button_visible = run_button.is_visible()
                    button_enabled = run_button.is_enabled()
                    if progress:
                        progress(
                            "MTBP SUBMISSION | "
                            f"analysis_id={analysis_id} | queries={len(submitted_queries)} | "
                            f"button_visible={'yes' if button_visible else 'no'} | "
                            f"button_enabled={'yes' if button_enabled else 'no'} | "
                            f"url={page.url}"
                        )
                    if not button_visible or not button_enabled:
                        raise RuntimeError(
                            "MTBP submission button was not ready; no new batch was clicked."
                        )
                    run_button.click()
                    submission_clicked = True
                    validation_text = self._wait_for_mtbp_acceptance(page)
                    if not validation_text:
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
                        fallback = _mtbp_retry_query(
                            variant,
                            query,
                            query_attempts[key],
                            unmapped,
                        )
                        if fallback and fallback != query:
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
                    failure_stage = "report_polling"
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
                failure_stage = "report_capture"
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
                for variant, query in active_pairs:
                    item = parsed[self.variant_key(variant)]
                    item.accession = query
                    item.raw.update({"analysis_id": analysis_id, "submitted_query": query})
                self._publish_mtbp_results(parsed)
                full_report_path: Path | None = None
                full_report_error = ""
                try:
                    full_report_path = self._capture_mtbp_full_report(
                        page,
                        artifact_directory,
                        analysis_id,
                    )
                except Exception as exc:
                    full_report_error = str(exc)
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
                audit_records: list[tuple[Path, VariantRecord, DatabaseEvidence]] = []
                for variant, query in active_pairs:
                    key = self.variant_key(variant)
                    evidence = parsed[key]
                    evidence.accession = query
                    screenshot_path = None
                    if evidence.status == "found" or (
                        full_report_path is not None and evidence.raw.get("gene_candidate_count", 0) > 0
                    ):
                        try:
                            screenshot_path = self._capture_mtbp_variant_with_fallback(
                                page, variant, artifact_directory, full_report_path
                            )
                        except (IncompleteCaptureError, OSError) as exc:
                            if evidence.status == "found":
                                evidence.status = "partial_capture"
                            evidence.summary = (
                                f"{evidence.summary} MTBP screenshot requires retry."
                            ).strip()
                            evidence.raw["capture_validation"] = {
                                "valid": False,
                                "reason": exc.validation.reason if isinstance(exc, IncompleteCaptureError) else str(exc),
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
                            "patient_report_screenshot": str(
                                full_report_path or ""
                            ),
                            "patient_report_capture_error": full_report_error,
                            "visible_text_preview": body_text[:12_000],
                        }
                    )
                    audit_path = self._screenshot_path(
                        artifact_directory, "MTBP", variant
                    ).with_suffix(".audit.json")
                    audit_records.append((audit_path, variant, evidence))
                    results[key] = evidence
                remote_cleanup = self._finalize_mtbp_report(
                    page,
                    analysis_id,
                    audit_records,
                )
                if progress:
                    if remote_cleanup["status"] == "deleted":
                        progress(
                            "MTBP: locally captured report deleted from portal "
                            f"({analysis_id})"
                        )
                    else:
                        progress(
                            "MTBP: remote report cleanup "
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
        except MtbpReportTimeout:
            current_url = context.pages[0].url if context and context.pages else self.login_url("MTBP")
            for variant, query in query_pairs:
                key = self.variant_key(variant)
                if key in results:
                    continue
                evidence = DatabaseEvidence(
                    "MTBP",
                    "timeout",
                    "MTBP did not produce a report before the configured "
                    f"{self.analysis_timeout_ms // 60_000}-minute timeout.",
                    accession=query,
                    url=current_url,
                    raw={
                        "analysis_id": analysis_id,
                        "submitted_query": query,
                        "query_attempts": query_attempts.get(key, [query]),
                    },
                )
                self._write_audit(
                    evidence,
                    self._screenshot_path(
                        artifact_directory, "MTBP", variant
                    ).with_suffix(".audit.json"),
                )
                results[key] = evidence
        except (browser_timeout, TimeoutError) as exc:
            current_url = context.pages[0].url if context and context.pages else self.login_url("MTBP")
            for variant, query in query_pairs:
                key = self.variant_key(variant)
                if key in results:
                    continue
                if failure_stage == "submission_acceptance" and submission_clicked:
                    evidence = _mtbp_uncertain_submission_evidence(
                        variant,
                        query,
                        analysis_id,
                        query_attempts.get(key, [query]),
                        current_url,
                        exc,
                    )
                else:
                    evidence = DatabaseEvidence(
                        "MTBP",
                        "error",
                        "An MTBP page operation timed out before report polling completed. "
                        f"The remote report may still be available in Reports List: {exc}",
                        accession=query,
                        url=current_url,
                        raw={
                            "analysis_id": analysis_id,
                            "submitted_query": query,
                            "query_attempts": query_attempts.get(key, [query]),
                            "failure_stage": failure_stage,
                        },
                    )
                self._write_audit(
                    evidence,
                    self._screenshot_path(
                        artifact_directory, "MTBP", variant
                    ).with_suffix(".audit.json"),
                )
                results[key] = evidence
        except Exception as exc:
            current_url = context.pages[0].url if context and context.pages else self.login_url("MTBP")
            for variant, query in query_pairs:
                key = self.variant_key(variant)
                if key in results:
                    continue
                if failure_stage == "submission_acceptance" and submission_clicked:
                    evidence = _mtbp_uncertain_submission_evidence(
                        variant,
                        query,
                        analysis_id,
                        query_attempts.get(key, [query]),
                        current_url,
                        exc,
                    )
                else:
                    status = "partial_capture" if failure_stage == "report_capture" else "error"
                    if failure_stage == "report_capture" and key in parsed:
                        evidence = deepcopy(parsed[key])
                        if evidence.status == "found":
                            evidence.status = "partial_capture"
                        evidence.summary += f" MTBP capture requires retry: {exc}"
                        evidence.raw["capture_error"] = str(exc)
                        evidence.raw["failure_stage"] = failure_stage
                    else:
                        evidence = DatabaseEvidence(
                            "MTBP",
                            status,
                            f"MTBP browser lookup failed while {failure_stage}: {exc}",
                            accession=query,
                            url=current_url,
                            raw={
                                "analysis_id": analysis_id,
                                "submitted_query": query,
                                "query_attempts": query_attempts.get(key, [query]),
                                "failure_stage": failure_stage,
                            },
                        )
                self._write_audit(
                    evidence,
                    self._screenshot_path(
                        artifact_directory, "MTBP", variant
                    ).with_suffix(".audit.json"),
                )
                results[key] = evidence
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
        protected_analysis_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Remove every stale app-generated report before a new submission."""
        self._goto_with_retries(page, MTBP_REPORTS_URL)
        remaining = page.locator("button.delete-patient").count()
        generated = page.locator(
            "button.delete-patient[data-patient-name^='ARCHER-']"
        )
        generated_count = generated.count()
        deleted: list[str] = []
        failed: list[str] = []
        generated_ids = [
            str(generated.nth(index).get_attribute("data-patient-name") or "")
            for index in range(generated_count)
        ]
        protected = protected_analysis_ids or set()
        for analysis_id in generated_ids:
            outcome = self._delete_mtbp_report(page, analysis_id)
            if outcome.get("status") not in {"deleted", "already_absent"}:
                failed.append(analysis_id)
                continue
            deleted.append(analysis_id)
            remaining = page.locator("button.delete-patient").count()
            if progress:
                progress(f"MTBP: removed old app report {analysis_id}")
        remaining = page.locator("button.delete-patient").count()
        generated_count = generated.count()
        if generated_count >= MTBP_ARCHER_REPORT_LIMIT:
            raise RuntimeError(
                f"MTBP still lists {generated_count} app-generated ARCHER report(s). "
                "The app allows only one active ARCHER report and requires zero "
                "before a new submission. Manual reports were left untouched."
            )
        outcome = {
            "status": "deleted_stale" if deleted else "retained",
            "trigger": "capacity" if deleted else "none",
            "deleted_stale_reports": deleted,
            "failed_deletions": failed,
            "protected_reports": sorted(protected),
            "remaining_reports": remaining,
            "remaining_archer_reports": generated_count,
        }
        if progress:
            progress(
                "MTBP PREFLIGHT | "
                f"reports={remaining} | archer_reports={generated_count} | "
                f"protected_reports={len(protected)} | "
                "available_archer_slots="
                f"{max(0, MTBP_ARCHER_REPORT_LIMIT - generated_count)} | "
                f"cleanup={outcome['status']}"
            )
        outcome["available_archer_slots"] = max(
            0, MTBP_ARCHER_REPORT_LIMIT - generated_count
        )
        return outcome

    def _finalize_mtbp_report(
        self,
        page: Any,
        analysis_id: str,
        audit_records: list[tuple[Path, VariantRecord, DatabaseEvidence]],
    ) -> dict[str, str]:
        """Persist local audit state before removing one exact ARCHER report."""
        try:
            for audit_path, variant, evidence in audit_records:
                self._persist_mtbp_report_audit(audit_path, variant, evidence)
        except OSError as exc:
            outcome = {"status": "pending", "message": f"Local audit could not be saved: {exc}"}
            for _, _, evidence in audit_records:
                evidence.raw["remote_report_cleanup"] = outcome
                evidence.raw["audit_write_error"] = str(exc)
            return outcome
        outcome = self._delete_mtbp_report(page, analysis_id)
        for audit_path, variant, evidence in audit_records:
            evidence.raw["remote_report_cleanup"] = outcome
            try:
                self._persist_mtbp_report_audit(audit_path, variant, evidence)
            except OSError as exc:
                evidence.raw["audit_write_error"] = str(exc)
        return outcome

    def _persist_mtbp_report_audit(
        self, audit_path: Path, variant: VariantRecord, evidence: DatabaseEvidence,
    ) -> None:
        """Checkpoint the identity-qualified audit used by resume before cleanup."""
        query_attempts = evidence.raw.get("query_attempts", [])
        write_evidence_audit(
            audit_path.parent, "MTBP", variant, evidence,
            query_attempts=query_attempts if isinstance(query_attempts, list) else [],
        )
        self._write_audit(evidence, audit_path)

    def _delete_mtbp_report(self, page: Any, analysis_id: str) -> dict[str, str]:
        """Delete one exact app-generated report and never touch manual report IDs."""
        if not analysis_id.startswith("ARCHER-"):
            return {
                "status": "skipped",
                "message": "Only ARCHER-prefixed reports may be deleted automatically.",
            }
        try:
            if page.url.rstrip("/") != MTBP_REPORTS_URL.rstrip("/"):
                self._goto_with_retries(page, MTBP_REPORTS_URL)
            report_link = page.get_by_role("link", name=analysis_id, exact=True)
            report_count = report_link.count()
            if report_count == 0:
                return {
                    "status": "already_absent",
                    "message": "The exact generated report was already absent.",
                }
            if report_count != 1:
                return {
                    "status": "failed",
                    "message": (
                        "The exact generated report was not unique in the report list."
                    ),
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
            for _ in range(MTBP_DELETE_VERIFY_ATTEMPTS):
                page.wait_for_timeout(MTBP_DELETE_SETTLE_MS)
                self._goto_with_retries(page, MTBP_REPORTS_URL, attempts=2)
                if page.get_by_role(
                    "link", name=analysis_id, exact=True
                ).count() == 0:
                    return {
                        "status": "deleted",
                        "message": (
                            "The generated MTBP report was removed and its absence "
                            "was verified after server settling time."
                        ),
                    }
            return {
                "status": "failed",
                "message": (
                    "MTBP still listed the generated report after three delayed "
                    "verification checks."
                ),
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
        with expanded_capture_layout(page, "table"):
            return BrowserReviewService._extract_expanded_mtbp_rows(page)

    @staticmethod
    def _extract_expanded_mtbp_rows(page: Any) -> list[dict[str, Any]]:
        """Read visible alteration tables and their exact linked identity details."""
        return page.locator("table").evaluate_all(MTBP_TABLE_ROWS_JS)

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
        # Franklin briefly leaves the previous Angular panel visible after the
        # subtab changes. A fixed render buffer avoids capturing those old pixels
        # without rejecting valid narrow/two-column classification layouts.
        page.wait_for_timeout(1_000)

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
                panel_selector="gnx-result-page",
                gene_symbol=variant.symbol,
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
                panel_selector="gnx-oncogenic-classification-app",
                gene_symbol=variant.symbol,
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
        self, page: Any, panel: Any, first_category: Any, screenshot_path: Path,
        *, panel_selector: str = "gnx-result-page", gene_symbol: str = "",
    ) -> None:
        with expanded_capture_layout(page, panel_selector):
            self._capture_franklin_expanded_overview(
                page, panel, first_category, screenshot_path, gene_symbol=gene_symbol
            )

    def _capture_franklin_expanded_overview(
        self,
        page: Any,
        panel: Any,
        first_category: Any,
        screenshot_path: Path,
        *,
        gene_symbol: str = "",
    ) -> None:
        """Capture the gene heading and summary before the first evidence category."""
        panel.evaluate("el => { el.scrollTop = 0; }")
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
        panel_box = panel.bounding_box()
        category_box = first_category.bounding_box()
        if panel_box is None or category_box is None:
            raise RuntimeError("Franklin classification overview was not visible.")

        panel_x = float(panel_box["x"])
        panel_y = float(panel_box["y"])
        panel_right = panel_x + float(panel_box["width"])
        header_box = self._franklin_gene_header_box(
            page, gene_symbol, panel_y, float(category_box["y"])
        )
        if header_box is None:
            target_left = 0
            target_top = 0
            target_right = panel_right
        else:
            header_x = float(header_box["x"])
            header_right = header_x + float(header_box["width"])
            target_left = min(panel_x, header_x)
            target_top = float(header_box["y"])
            target_right = max(panel_right, header_right)
        target_bottom = float(category_box["y"])
        if target_bottom - target_top <= 1:
            raise RuntimeError("Franklin classification overview could not be cropped.")
        try:
            document_box = page.evaluate(
                "() => ({x: 0, y: 0, width: document.documentElement.scrollWidth, "
                "height: document.documentElement.scrollHeight})"
            )
        except (AttributeError, TypeError):
            document_box = None
        if (
            not isinstance(document_box, dict)
            or "width" not in document_box
            or "height" not in document_box
        ):
            document_box = {
                "x": 0,
                "y": 0,
                "width": target_right + 32,
                "height": target_bottom,
            }
        clip = _expanded_capture_box(
            {
                "x": target_left,
                "y": target_top,
                "width": target_right - target_left,
                "height": target_bottom - target_top,
            },
            document_box,
            bottom_margin=0,
        )
        page.screenshot(
            path=str(screenshot_path),
            clip=clip,
        )

    @staticmethod
    def _franklin_gene_header_box(
        page: Any,
        gene_symbol: str,
        panel_y: float,
        category_y: float,
    ) -> dict[str, float] | None:
        if not gene_symbol:
            return None
        try:
            matches = page.get_by_text(gene_symbol, exact=True)
            candidates = []
            for index in range(matches.count()):
                box = matches.nth(index).bounding_box()
                if box is None:
                    continue
                top = float(box["y"])
                bottom = top + float(box["height"])
                if top <= panel_y + 40 and bottom < category_y:
                    candidates.append(box)
            return max(candidates, key=lambda box: float(box["y"]), default=None)
        except (AttributeError, TypeError):
            return None

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

    def _capture_franklin_assessment_with_retry(
        self, page: Any, variant: VariantRecord, artifact_directory: Path,
    ) -> list[dict[str, str]]:
        """Reload only the resolved assessment view once after a transient failure."""
        for attempt in range(2):
            self._check_cancelled()
            try:
                dismiss_franklin_onboarding(page)
                return self._capture_franklin_assessment(page, variant, artifact_directory)
            except Exception as exc:
                if attempt or not _transient_browser_failure(exc):
                    raise
                self._interruptible_page_wait(page, 750)
        raise RuntimeError("Franklin assessment retry ended unexpectedly.")

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

    def _capture_mtbp_variant_with_fallback(
        self,
        page: Any,
        variant: VariantRecord,
        artifact_directory: Path,
        full_report_path: Path | None,
    ) -> Path:
        if full_report_path is None:
            return self._capture_mtbp_variant_screenshot(page, variant, artifact_directory)
        try:
            return self._crop_mtbp_variant_from_report(
                page, variant, artifact_directory, full_report_path
            )
        except IncompleteCaptureError as crop_error:
            try:
                return self._capture_mtbp_variant_screenshot(
                    page, variant, artifact_directory
                )
            except IncompleteCaptureError as direct_error:
                raise IncompleteCaptureError(
                    CaptureValidation(
                        False,
                        f"{crop_error.validation.reason}; direct:{direct_error.validation.reason}",
                        0, 0, 0.0,
                    )
                ) from direct_error

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
                shared = [
                    entry for entry in self._extract_mtbp_rows(page)
                    if entry.get("capture_geometry", {}).get("shared_gene_context")
                    and _mtbp_screenshot_row_matches(
                        str(entry.get("gene", "")) + " " + _mtbp_row_identity_text(entry), variant,
                    )
                ]
                if len(shared) == 1:
                    # A physical tr may omit its inherited gene label. Freeze the
                    # full visible context and mark the crop as gene context.
                    context_path = self._capture_mtbp_full_report(
                        page, artifact_directory, screenshot_path.stem + "-shared-gene",
                    )
                    return self._crop_mtbp_variant_from_report(page, variant, artifact_directory, context_path)
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

    def _crop_mtbp_variant_from_report(
        self, page: Any, variant: VariantRecord, artifact_directory: Path,
        full_report_path: Path,
    ) -> Path:
        """Reuse frozen report pixels and retain match scope in PNG metadata."""
        try:
            geometry = json.loads(full_report_path.with_suffix(".geometry.json").read_text(encoding="utf-8"))
            gene_rows = [entry for entry in geometry["rows"]
                         if variant.symbol and _mtbp_gene_symbol(entry["gene"]).casefold() == variant.symbol.casefold()]
            matches = [entry for entry in gene_rows
                       if _mtbp_screenshot_row_matches(entry["gene"] + " " + entry["identity"], variant)]
            selected = matches if len(matches) == 1 else gene_rows
            gene_context = len(matches) != 1 or any(entry.get("shared_gene_context") for entry in selected)
            if not selected:
                raise ValueError("MTBP capture has no rows for this gene")
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("match_scope", "gene_context" if gene_context else "exact_variant")
            path = self._screenshot_path(artifact_directory, "MTBP", variant)
            with Image.open(full_report_path) as source:
                sx, sy = source.width / geometry["width"], source.height / geometry["height"]
                pieces = []
                # Repeat the section and column heading for each selected row,
                # preserving putative/unknown context and the complete A/B/C cell.
                boxes = [box for entry in selected
                         for box in (entry.get("section"), entry.get("header"), entry["row"])]
                for box in boxes:
                    if not box:
                        continue
                    bounds = (round(box["x"] * sx), round(box["y"] * sy),
                              round((box["x"] + box["width"]) * sx),
                              round((box["y"] + box["height"]) * sy))
                    if (bounds[0] < 0 or bounds[1] < 0 or bounds[2] > source.width
                            or bounds[3] > source.height or bounds[2] <= bounds[0]
                            or bounds[3] <= bounds[1]):
                        raise ValueError("MTBP row outside captured report")
                    pieces.append(source.crop(bounds).convert("RGB"))
                width = max(p.width for p in pieces)
                combined = Image.new("RGB", (width, sum(p.height for p in pieces)), "white")
                y = 0
                for piece in pieces:
                    combined.paste(piece, (0, y))
                    y += piece.height
                combined.save(path, pnginfo=metadata)
            validation = self.capture_validator(path)
            if not validation.valid:
                raise IncompleteCaptureError(validation)
            return path
        except IncompleteCaptureError:
            raise
        except Exception as exc:
            raise IncompleteCaptureError(CaptureValidation(False, f"mtbp_crop:{exc}", 0, 0, 0.0)) from exc

    def _capture_mtbp_full_report(
        self,
        page: Any,
        artifact_directory: Path,
        analysis_id: str,
    ) -> Path:
        artifact_directory.mkdir(parents=True, exist_ok=True)
        safe_analysis_id = re.sub(r"[^A-Za-z0-9_-]+", "_", analysis_id).strip("_")
        screenshot_path = artifact_directory / f"{safe_analysis_id}-full-report.png"

        def capture() -> None:
            # Freeze geometry with the capture, so later scrolling/accordion
            # changes cannot assign another variant's pixels to this variant.
            geometry = page.evaluate("""() => {
                const rows = (TABLE_ROWS)([...document.querySelectorAll('table')])
                    .map(entry => entry.capture_geometry);
                const genomics = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                    .find(node => node.innerText.trim() === 'Genomics'
                        && node.getBoundingClientRect().height > 0);
                const fallbackTop = rows.length
                    ? Math.min(...rows.map(entry =>
                        (entry.section || entry.header || entry.row).y))
                    : 0;
                return {width:document.documentElement.scrollWidth,
                    height:document.documentElement.scrollHeight,
                    content_top: genomics ? genomics.getBoundingClientRect().y + scrollY : fallbackTop,
                    rows};
            }""".replace("TABLE_ROWS", MTBP_TABLE_ROWS_JS))
            page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot_path.with_suffix(".geometry.json").write_text(
                json.dumps(geometry), encoding="utf-8"
            )

        with expanded_capture_layout(page, "table"):
            self._capture_with_incident_retry(page, screenshot_path, capture)
        return screenshot_path

    def _locate_mtbp_screenshot_target(
        self, page: Any, variant: VariantRecord
    ) -> tuple[Any, Any]:
        rows = page.locator("table tr")
        report_rows = self._extract_mtbp_rows(page)
        matches = [
            entry["row_index"] for entry in report_rows
            if _mtbp_screenshot_row_matches(
                str(entry.get("gene", "")) + " " + _mtbp_row_identity_text(entry), variant
            )
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
        return row, row

    @staticmethod
    def _screenshot_label(database: str) -> str:
        if database == "OncoKB":
            return "Variant overview and mutation effect"
        if database == "Franklin":
            return "Franklin classification and ACMG evidence"
        return "Provider evidence"

    def _next_request_delay_ms(self, database: str) -> int:
        if database == "COSMIC":
            low, high = COSMIC_REQUEST_DELAY_MS, COSMIC_REQUEST_DELAY_MAX_MS
        else:
            low, high = self.request_delay_ms, self.request_delay_max_ms
        if high <= 0 or high <= low:
            return max(0, low)
        return random.randint(low, high)

    def _wait_between_queries(
        self,
        page: Any,
        database: str,
        *,
        progress: Callable[[str], None] | None,
    ) -> None:
        delay_ms = self._next_request_delay_ms(database)
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
            lowered = body_text.casefold()
            if re.search(
                r"\b(?:please|must|need to)\s+(?:log in|login|sign in)\s+"
                r"(?:to\s+)?(?:view|access|continue)", lowered,
            ):
                raise ProviderLookupError(
                    ProviderFailureKind.LOGIN_REQUIRED,
                    "OncoKB requires sign-in to view this result; refresh the session before retrying.",
                    diagnostics={"failure_stage": "session_required"},
                )
            diagnostics: dict[str, object] = {
                "page_type": "variant",
                "variant_overview": "variant overview" in lowered,
                "mutation_effect": "mutation effect" in lowered,
            }
            if diagnostics["variant_overview"] and diagnostics["mutation_effect"]:
                return
            if (
                "page not found" in lowered
                or "an error has occurred" in lowered
                or "we do not have any information for this gene" in lowered
                or "we do not have any information for this variant" in lowered
                or "reference amino acid at position" in lowered
                or re.search(r"\binvalid\s+somatic\b", body_text, re.IGNORECASE)
            ):
                return
            self._interruptible_page_wait(page, 500)
        raise _OncoKBRenderingTimeout(diagnostics)

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


def _expanded_capture_box(
    target_box: dict[str, float],
    document_box: dict[str, float],
    horizontal_margin: int = 32,
    top_margin: int = 24,
    bottom_margin: int = 24,
) -> dict[str, float]:
    document_left = float(document_box.get("x", 0))
    document_top = float(document_box.get("y", 0))
    document_right = document_left + float(document_box["width"])
    document_bottom = document_top + float(document_box["height"])
    target_left = float(target_box["x"])
    target_top = float(target_box["y"])
    target_right = target_left + float(target_box["width"])
    target_bottom = target_top + float(target_box["height"])
    left = max(document_left, target_left - horizontal_margin)
    top = max(document_top, target_top - top_margin)
    right = min(document_right, target_right + horizontal_margin)
    bottom = min(document_bottom, target_bottom + bottom_margin)
    return {
        "x": left,
        "y": top,
        "width": max(0.0, right - left),
        "height": max(0.0, bottom - top),
    }


def _cosmic_migration_target(text: str, original: str) -> str:
    """Accept directed provider notices, never unrelated IDs or internal page IDs."""
    normalized = " ".join(text.split())
    matches = re.findall(
        rf"\b{re.escape(original)}\b\s+(?:has\s+(?:now\s+)?been\s+|was\s+|is\s+)?"
        r"(?:merged\s+(?:into|with)|migrated\s+to|replaced\s+by|redirected\s+to)"
        r"\s+(?:mutation\s+)?(?:ID\s*:?\s*)?(COS[MV]\d+)\b",
        normalized, flags=re.I,
    )
    # COSMIC v104 puts the destination in its labelled overview fields rather
    # than in the merge sentence. Bind those fields to this exact source ID.
    matches.extend(re.findall(
        rf"\bThe legacy mutation\s+{re.escape(original)}\s+has now been merged into the following mutation\.\s*"
        r"Genomic Mutation ID\s+COSV\d+\s+Legacy Identifier\s+(COSM\d+)\b",
        normalized, flags=re.I,
    ))
    targets = {target.upper() for target in matches if target.upper() != original.upper()}
    if len(targets) > 1:
        raise ProviderLookupError(ProviderFailureKind.AMBIGUOUS, "COSMIC exposed multiple replacement IDs.")
    if not targets:
        raise ProviderLookupError(ProviderFailureKind.NOT_FOUND, f"No explicit COSMIC ID migration was found for {original}.")
    return targets.pop()


def _cosmic_identifier(value: str | None) -> str:
    identifiers = _cosmic_identifiers(value)
    return identifiers[0] if identifiers else ""


def _transient_browser_failure(exc: Exception) -> bool:
    """Limit automatic query replays to timeouts and recoverable network errors."""
    return isinstance(exc, (TimeoutError, ConnectionError)) or bool(
        re.search(
            r"\bnet::ERR_(?:CONNECTION_RESET|CONNECTION_CLOSED|CONNECTION_ABORTED|"
            r"TIMED_OUT|NETWORK_CHANGED|NAME_NOT_RESOLVED|INTERNET_DISCONNECTED)\b",
            str(exc), re.I,
        )
    )


def _cosmic_identifiers(value: str | None) -> list[str]:
    identifiers: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\bCOS(?:M|V)\d+\b", value or "", re.IGNORECASE):
        identifier = match.group(0).upper()
        if identifier not in seen:
            seen.add(identifier)
            identifiers.append(identifier)
    return identifiers


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


def _log_field(value: object, *, maximum: int = 160) -> str:
    """Keep progress events single-line and stable enough to filter or parse."""
    normalized = " ".join(str(value or "").replace("|", "/").split())
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1].rstrip() + "…"


def _oncokb_query_urls(variant: VariantRecord) -> list[str]:
    candidates: list[str] = []
    protein = _protein_change(variant.hgvsp)
    usable_protein = protein if protein not in {"", "?", "=", "-", "."} else ""
    alteration = usable_protein or _cdna_change(variant.hgvsc)
    if variant.symbol and alteration:
        candidates.append(
            "https://www.oncokb.org/gene/"
            f"{quote(variant.symbol, safe='')}/somatic/{quote(alteration, safe='')}"
        )
    genomic_hgvs = format_mtbp_grch37(
        variant.genomic_location, variant.ref_allele, variant.alt_allele
    )
    if genomic_hgvs.startswith("chr"):
        genomic_hgvs = genomic_hgvs[3:]
    if genomic_hgvs:
        candidates.append(
            "https://www.oncokb.org/hgvsg/"
            f"{quote(genomic_hgvs, safe=':.')}?refGenome=GRCh37"
        )
    if not usable_protein:
        candidates.sort(key=lambda url: "/hgvsg/" not in url)
    return list(dict.fromkeys(candidates))


def _oncokb_page_matches_variant(
    body_text: str, variant: VariantRecord, url: str
) -> bool:
    expected_hgvsg = format_mtbp_grch37(
        variant.genomic_location, variant.ref_allele, variant.alt_allele
    )
    if expected_hgvsg.startswith("chr"):
        expected_hgvsg = expected_hgvsg[3:]
    if "/hgvsg/" in url.casefold() and expected_hgvsg:
        parsed_url = urlsplit(url)
        reference_genomes = [
            value.casefold()
            for key, values in parse_qs(parsed_url.query).items()
            if key.casefold() == "refgenome"
            for value in values
        ]
        return (
            unquote(parsed_url.path).rstrip("/") == f"/hgvsg/{expected_hgvsg}"
            and reference_genomes == ["grch37"]
        )
    protein = _protein_change(variant.hgvsp)
    alteration = (
        protein if protein not in {"", "?", "=", "-", "."}
        else _cdna_change(variant.hgvsc)
    )
    return bool(
        variant.symbol and alteration and re.search(
            r"(?<![A-Za-z0-9_])" + re.escape(variant.symbol)
            + r"[\s:(),;/|\-]*" + re.escape(alteration)
            + r"(?![A-Za-z0-9_*>+\-])", body_text or "", re.I,
        )
    )


def _contains_hgvs_change(text: str, change: str) -> bool:
    if not change:
        return False
    pattern = r"\s*".join(re.escape(character) for character in change)
    return bool(re.search(
        r"(?<![A-Za-z0-9_])" + pattern + r"(?![A-Za-z0-9_*>+\-])",
        text or "", re.I,
    ))


def _clinvar_queries(variant: VariantRecord) -> list[str]:
    queries: list[str] = []
    identity = genomic_identity(variant)
    indel = bool(identity and len(identity.reference) != len(identity.alternate))
    if indel and identity and all(re.fullmatch(r"[ACGT]+", allele) for allele in (
        identity.reference, identity.alternate
    )):
        # NCBI's exact allele search supports VCF anchors and an explicit assembly.
        queries.append(
            f"{identity.chromosome}:{identity.position}:{identity.reference}:"
            f"{identity.alternate}(GRCh37)"
        )
    protein_query = _clinvar_protein_query(variant)
    if protein_query:
        queries.append(protein_query)
    if variant.hgvsc:
        queries.append(variant.hgvsc.strip())
    if (indel or not protein_query) and variant.symbol and variant.hgvsc:
        queries.append(f"{variant.symbol.strip()} {_cdna_change(variant.hgvsc)}")
    if identity and variant.symbol:
        queries.append(
            f"{variant.symbol.strip()}[gene] AND {identity.chromosome}[chr] AND "
            f"{identity.position}[chrpos37]"
        )
    return list(dict.fromkeys(query for query in queries if query))


def _clinvar_protein_query(variant: VariantRecord) -> str:
    protein = _protein_change(variant.hgvsp)
    # Unknown and frameshift protein descriptions are poor exact search keys.
    return (
        f"{variant.symbol.strip()} {protein}"
        if variant.symbol and re.fullmatch(r"[A-Z]\d+[A-Z*]", protein) else ""
    )


def _clinvar_protein_matches(text: str, variant: VariantRecord) -> bool:
    expected = _protein_change(variant.hgvsp).casefold()
    if not expected:
        return False
    return any(
        _protein_change(match.group(0)).casefold() == expected
        for match in re.finditer(
            r"\bp\.\(?[A-Za-z*]+\d+[A-Za-z*]+\)?(?=$|[^A-Za-z0-9])",
            text, re.I,
        )
    )


def _clinvar_locations(body_text: str) -> list[tuple[str, int, int, str]]:
    """Parse complete assembly-labelled coordinates, including VCF spans."""
    locations = []
    for match in re.finditer(
        r"\b(?:chr)?(\d{1,2}|X|Y|M|MT):\s*(\d+(?:,\d{3})*)"
        r"(?:\s*[-\u2013\u2014]\s*(\d+(?:,\d{3})*))?\s*\((GRCh3[78])\)",
        body_text, re.I,
    ):
        chromosome, start, end, assembly = match.groups()
        start_position = int(start.replace(",", ""))
        end_position = int((end or start).replace(",", ""))
        if 0 < start_position <= end_position:
            locations.append((
                "M" if chromosome.upper() == "MT" else chromosome.upper(),
                start_position, end_position, assembly.casefold(),
            ))
    return locations


def _clinvar_location_matches(body_text: str, expected: GenomicIdentity) -> bool:
    # Require the full reference span or its exact anchor-trimmed counterpart.
    # Mere overlap is never enough, and repeats are not shifted without proof.
    reference, alternate = expected.reference, expected.alternate
    if not all(re.fullmatch(r"[ACGT]+", allele) for allele in (reference, alternate)):
        return False
    spans = {(expected.position, expected.position + len(reference) - 1)}
    position = expected.position
    while reference and alternate and reference[-1] == alternate[-1]:
        reference, alternate = reference[:-1], alternate[:-1]
    while reference and alternate and reference[0] == alternate[0]:
        reference, alternate = reference[1:], alternate[1:]
        position += 1
    if not reference and not alternate:
        return False
    if reference:
        spans.add((position, position + len(reference) - 1))
    else:
        spans.add((position - 1, position))  # Insertion flanks, not affected bases.
    return any(
        chromosome == expected.chromosome and assembly == expected.assembly.casefold()
        and (start, end) in spans
        for chromosome, start, end, assembly in _clinvar_locations(body_text)
    )


def _clinvar_identity(
    body_text: str, variant: VariantRecord, *, allow_protein: bool = False
) -> IdentityVerification:
    expected = genomic_identity(variant)
    if expected is None:
        return IdentityVerification(False, "none", "GRCh37 input identity is incomplete.")
    compact = re.sub(r"\s+", "", body_text or "").casefold()
    if not _clinvar_location_matches(body_text, expected):
        return IdentityVerification(
            False, "grch37_location", "ClinVar page did not show the requested GRCh37 location."
        )
    cdna = _cdna_change(variant.hgvsc).casefold()
    symbol = (variant.symbol or "").strip()
    transcript = (variant.hgvsc or "").split(":", 1)[0].strip().casefold()
    if not symbol or not re.search(rf"\b{re.escape(symbol)}\b", body_text, re.I):
        return IdentityVerification(
            False, "variant_change", "ClinVar page did not show the requested gene."
        )
    cdna_matches = _contains_hgvs_change(body_text, cdna)
    if cdna_matches and transcript and transcript in compact:
        return IdentityVerification(
            True,
            "exact_transcript_grch37",
            "Transcript cDNA and GRCh37 location matched.",
            expected,
        )
    if cdna_matches:
        return IdentityVerification(
            True, "gene_cdna_grch37",
            "Gene, cDNA change, and GRCh37 location matched.", expected,
        )
    if allow_protein and _clinvar_protein_matches(body_text, variant):
        if cdna and re.search(r"\bc\.[^\s(),;]+", body_text, re.I):
            return IdentityVerification(
                False, "variant_change",
                "ClinVar showed a different cDNA change at the protein-matched location.",
            )
        return IdentityVerification(
            True, "gene_protein_grch37",
            "Gene, protein change, and GRCh37 location matched.", expected,
        )
    return IdentityVerification(
        False, "variant_change",
        "ClinVar page did not show the requested cDNA or protein change.",
    )


def _matching_clinvar_links(
    links: object, variant: VariantRecord, *, allow_protein: bool = False
) -> list[str]:
    if not isinstance(links, list):
        return []
    cdna = _cdna_change(variant.hgvsc).casefold()
    symbol = (variant.symbol or "").strip()
    candidates: list[str] = []
    for item in links:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        href = str(item.get("href") or "").strip()
        matched_change = _contains_hgvs_change(text, cdna)
        if allow_protein:
            matched_change = matched_change or _clinvar_protein_matches(
                str(item.get("text") or ""), variant
            )
        gene_matches = bool(
            symbol and re.search(rf"\b{re.escape(symbol)}\b", str(item.get("text") or ""), re.I)
        )
        if href and gene_matches and matched_change:
            candidates.append(href)
    return list(dict.fromkeys(candidates))


def parse_oncokb_page(
    body_text: str, variant: VariantRecord, url: str
) -> DatabaseEvidence:
    oncogenicity = _after_heading(body_text, "Oncogenicity")
    biological_effect = _after_heading(body_text, "Biological Effect")
    overview = _between(body_text, "Variant Overview", "Mutation Effect")
    page_identity = f"{variant.symbol} {_protein_change(variant.hgvsp)}".strip()
    if "an error has occurred" in body_text.casefold():
        return DatabaseEvidence(
            "OncoKB",
            "error",
            f"OncoKB provider error while loading {page_identity}; retry is allowed.",
            accession=page_identity,
            url=url,
            raw={"failure_kind": ProviderFailureKind.TRANSIENT.value},
        )
    canonical_mismatch = re.search(
        r"[^\n]*The reference amino acid at position\s+\d+\s+is\s+[^\n]+?"
        r"on the OncoKB canonical transcript\.?",
        body_text,
        flags=re.IGNORECASE,
    )
    if canonical_mismatch:
        detail = " ".join(canonical_mismatch.group(0).split())
        return DatabaseEvidence(
            "OncoKB",
            "not_found",
            f"OncoKB canonical transcript mismatch for {page_identity}: {detail}",
            accession=page_identity,
            url=url,
            raw={"failure_kind": ProviderFailureKind.IDENTITY_MISMATCH.value},
        )
    if "we do not have any information for this gene" in body_text.casefold():
        return DatabaseEvidence(
            "OncoKB",
            "not_found",
            f"OncoKB has no information for gene {variant.symbol}.",
            accession=page_identity,
            url=url,
            raw={"failure_kind": ProviderFailureKind.NOT_FOUND.value},
        )
    if "we do not have any information for this variant" in body_text.casefold():
        return DatabaseEvidence(
            "OncoKB", "not_found", "OncoKB explicitly reported no information for this variant.",
            accession=page_identity, url=url,
            raw={"failure_kind": ProviderFailureKind.NOT_FOUND.value},
        )
    if not oncogenicity and "Variant Overview" not in body_text:
        return DatabaseEvidence(
            "OncoKB", "not_found", f"No OncoKB web result for {page_identity}.",
            accession=page_identity, url=url,
            raw={"failure_kind": ProviderFailureKind.NOT_FOUND.value},
        )
    if not _oncokb_page_matches_variant(body_text, variant, url):
        return DatabaseEvidence(
            "OncoKB",
            "identity_mismatch",
            f"OncoKB page did not match the requested variant {page_identity}.",
            accession=page_identity,
            url=url,
            raw={"failure_kind": ProviderFailureKind.IDENTITY_MISMATCH.value},
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
    complete_submission: bool = True,
) -> dict[str, DatabaseEvidence]:
    """Map the MTBP alteration-centric table to submitted variants.

    Matching is deliberately strict. A gene-only match is accepted only when
    the complete submission contains one distinct variant for that gene and
    MTBP displays one row without variant identity. Retained reports may have
    incomplete submission context and cannot use this fallback.
    """
    variants = list(variants)
    submitted_by_gene: dict[str, set[tuple[str, ...]]] = {}
    for variant in variants:
        submitted_by_gene.setdefault(variant.symbol.casefold(), set()).add((
            variant.sample, variant.hgvsc, variant.hgvsp, variant.transcript,
            variant.genomic_location, variant.ref_allele, variant.alt_allele,
        ))
    metadata = {
        **_mtbp_report_metadata(body_text, cancer_type),
        "report_row_count": len(report_rows),
        "identity_matching_version": MTBP_MATCHING_REVISION,
    }
    if not report_rows and re.search(
        r"(?:Putative functionally (?:relevant|neutral) variants|"
        r"Variants of unknown/contradictory functional significance):\s*[1-9]\d*",
        body_text, re.I,
    ):
        return {
            BrowserReviewService.variant_key(variant): DatabaseEvidence(
                "MTBP", "layout_changed",
                "MTBP reported variants but no alteration table rows could be read; "
                "this is a report extraction error, not confirmed absence.",
                accession=_mtbp_variant_query(variant), url=url,
                raw={**metadata, "failure_stage": "report_row_extraction",
                    "failure_kind": ProviderFailureKind.LAYOUT_CHANGED.value},
            )
            for variant in variants
        }
    results: dict[str, DatabaseEvidence] = {}
    for variant in variants:
        key = BrowserReviewService.variant_key(variant)
        gene_candidates = [
            row for row in report_rows
            if _mtbp_gene_symbol(str(row.get("gene", ""))).casefold()
            == variant.symbol.casefold()
        ]
        expected_protein = _mtbp_normalized_protein(_protein_change(variant.hgvsp))
        expected_cdna = _cdna_change(variant.hgvsc).casefold()
        submitted_count = len(submitted_by_gene[variant.symbol.casefold()])
        if not (expected_protein or expected_cdna or _mtbp_genomic_query(variant)):
            results[key] = DatabaseEvidence(
                "MTBP", "invalid_query", "No submitted variant identity is available for MTBP matching.",
                url=url, raw={**metadata, "submitted_gene_variant_count": submitted_count},
            )
            continue
        matching_rows = [
            (row, basis) for row in gene_candidates
            if (basis := _mtbp_identity_match_basis(_mtbp_row_identity_text(row), variant))
        ]
        candidates = [row for row, _ in matching_rows]
        match_basis = (
            matching_rows[0][1] if matching_rows else
            "protein" if expected_protein else "cdna" if expected_cdna else "genomic_grch37"
        )
        unidentified_rows = [row for row in gene_candidates if not _mtbp_row_has_variant_identity(row)]
        if (
            not candidates and not expected_protein and expected_cdna
            and len(gene_candidates) == 1 and unidentified_rows
            and complete_submission and submitted_count == 1
        ):
            candidates = unidentified_rows
            match_basis = "unique_gene_without_reported_identity"

        if len(candidates) != 1:
            unresolved = bool(candidates or unidentified_rows)
            status = "manual_review" if unresolved else "not_found"
            detail = (
                f"{len(candidates)} possible rows" if candidates else
                "gene rows without enough variant identity" if unidentified_rows else "no matching row"
            )
            results[key] = DatabaseEvidence(
                "MTBP",
                status,
                f"MTBP report returned {detail} for {_mtbp_variant_query(variant)}.",
                accession=_mtbp_variant_query(variant),
                url=url,
                raw={
                    **metadata,
                    "lookup_status": status,
                    "candidate_count": len(candidates),
                    "gene_candidate_count": len(gene_candidates),
                    "submitted_gene_variant_count": submitted_count,
                    "complete_submission": complete_submission,
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
                "lookup_status": "found",
                "gene": row.get("gene", ""),
                "gene_info": row.get("gene_info", ""),
                "alteration": row.get("alteration", ""),
                "functional_relevance": classification,
                "functional_evidence": functional_evidence,
                "actionability_tiers": actionability_tiers,
                "biomarkers": biomarkers,
                "source_links": list(dict.fromkeys(row.get("source_links", []))),
                "submitted_gene_variant_count": submitted_count,
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
    if variant.symbol and not re.search(rf"\b{re.escape(variant.symbol)}\b", body_text or "", re.I):
        return IdentityVerification(False, "none", "Gene symbol did not match.")
    expected = genomic_identity(variant)
    returned = _franklin_genomic_identity(body_text, url)
    if expected and returned and expected != returned:
        return IdentityVerification(
            False,
            "grch37_genomic",
            "Returned GRCh37 genomic identity differed from the requested variant.",
            returned,
        )
    cdna = _cdna_change(variant.hgvsc)
    if _contains_hgvs_change(body_text, cdna):
        return IdentityVerification(
            True,
            "exact_transcript",
            "Transcript cDNA matched exactly.",
        )
    if expected and returned:
        return IdentityVerification(
            True,
            "grch37_genomic",
            "GRCh37 chromosome, position, reference, and alternate matched.",
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


def _mtbp_uncertain_submission_evidence(
    variant: VariantRecord,
    query: str,
    analysis_id: str,
    query_attempts: list[str],
    url: str,
    error: Exception,
) -> DatabaseEvidence:
    """Persist enough identity to reconcile an MTBP click with an unknown outcome."""
    return DatabaseEvidence(
        "MTBP",
        "submission_unknown",
        "MTBP did not confirm whether the patient batch was accepted. New MTBP "
        "submissions are paused until this exact analysis ID is reconciled in "
        f"Reports List ({error}).",
        accession=query or _review_query(variant),
        url=url,
        raw={
            "analysis_id": analysis_id,
            "submitted_query": query,
            "query_attempts": list(query_attempts),
            "failure_stage": "submission_acceptance",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "remote_report_recovery": {"status": "pending"},
        },
    )


def _mtbp_genomic_query(variant: VariantRecord) -> str:
    """Convert an Archer GRCh37 VCF-style allele into genomic HGVS.

    Archer represents indels with an anchored reference base. MTBP expects the
    inserted/deleted sequence itself in HGVS, so a simple ``REF>ALT`` string is
    only correct for substitutions.
    """
    return format_mtbp_grch37(
        variant.genomic_location,
        variant.ref_allele,
        variant.alt_allele,
    )


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


def _mtbp_retry_query(
    variant: VariantRecord,
    current_query: str,
    query_attempts: list[str],
    rejected: list[str],
) -> str:
    """Keep accepted queries and replace only an explicitly rejected query."""
    if not _mtbp_query_rejected(current_query, rejected):
        return current_query
    fallback = _mtbp_genomic_query(variant)
    if fallback and fallback not in query_attempts:
        return fallback
    return ""


def _mtbp_normalized_protein(value: str) -> str:
    clean = (value or "").replace("p.", "").replace("(", "").replace(")", "").strip()
    if clean in {"", "?", "=", "-", "."}:
        return ""
    # Normalize all amino-acid tokens, including ranges and frameshifts, not
    # only simple substitutions. Keep positions and stop distances intact.
    clean = re.sub(
        r"(?:" + "|".join(_AMINO_ACID_3_TO_1) + r"|Ter)",
        lambda match: "*" if match.group(0) == "Ter" else _AMINO_ACID_3_TO_1[match.group(0)],
        clean,
    )
    return clean.casefold()


def _mtbp_gene_symbol(value: str) -> str:
    match = re.search(r"[A-Za-z0-9-]+", value or "")
    return match.group(0) if match else ""


def _mtbp_row_has_variant_identity(row: dict[str, Any]) -> bool:
    text = _mtbp_row_identity_text(row)
    if _mtbp_proteins(text):
        return True
    return bool(re.search(r"\b[cnrmg]\.\s*[-+*0-9]", text, flags=re.IGNORECASE))


def _mtbp_row_identity_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(field, "")) for field in ("identity_text", "alteration"))


def _mtbp_proteins(text: str) -> set[str]:
    tokens = re.findall(r"p\.\(?([A-Za-z0-9_*?=]+)\)?", text or "")
    return {protein for token in tokens if (protein := _mtbp_normalized_protein(token))}


def _mtbp_row_genomic_matches(text: str, variant: VariantRecord) -> bool:
    return _mtbp_row_genomic_state(text, variant) is True


def _mtbp_row_genomic_state(text: str, variant: VariantRecord) -> bool | None:
    """True for exact GRCh37, False for explicit conflict, None if unreported."""
    reported = [
        (f"chr{'M' if chromosome.upper() == 'MT' else chromosome.upper()}:{change}".casefold(), assembly.casefold())
        for chromosome, change, assembly in re.findall(
            r"(?<![A-Za-z0-9_])(?:chr)?(\d{1,2}|X|Y|MT|M):\s*"
            r"(g\.[^\s();,]+)\s*\((GRCh3[78])\)", text or "", re.I,
        )
    ]
    if not reported:
        return None
    expected = _mtbp_genomic_query(variant)
    if not expected:
        return False
    grch37 = [change for change, assembly in reported if assembly == "grch37"]
    return bool(grch37) and all(change == expected.casefold() for change in grch37)


def _mtbp_row_cdna_matches(
    text: str, variant: VariantRecord, *, require_transcript: bool = False,
) -> bool:
    expected = _cdna_change(variant.hgvsc)
    if not _contains_hgvs_change(text, expected):
        return False
    transcript_pattern = r"(?:(?:NM|NR|XM|XR)_\d+|ENST\d+)(?:\.\d+)?"
    qualified_input = re.match(r"(" + transcript_pattern + r")\s*:", variant.hgvsc, re.I)
    accession = qualified_input.group(1) if qualified_input else variant.transcript
    transcript = (accession or "").split(".", 1)[0].casefold()
    qualified = re.findall(
        r"\b(" + transcript_pattern + r")(?:\s*\([^)]+\))?\s*:\s*"
        r"([cn]\.[^\s();,]+)", text or "", re.I,
    )
    if qualified:
        if not re.fullmatch(transcript_pattern, accession or "", re.I):
            return False
        return any(
            accession.split(".", 1)[0].casefold() == transcript
            and _contains_hgvs_change(change, expected)
            for accession, change in qualified
        )
    return not require_transcript


def _mtbp_screenshot_row_matches(text: str, variant: VariantRecord) -> bool:
    if not variant.symbol or not re.search(
        r"(?<![A-Za-z0-9_-])" + re.escape(variant.symbol) + r"(?![A-Za-z0-9_-])",
        text or "", re.I,
    ):
        return False
    return bool(_mtbp_identity_match_basis(text, variant))


def _mtbp_identity_match_basis(text: str, variant: VariantRecord) -> str:
    """Use the same conservative identity rules for evidence and its capture."""
    genomic_state = _mtbp_row_genomic_state(text, variant)
    if genomic_state is False:
        return ""
    # A reported protein conflict must not be overridden by DNA fallback. When
    # the report omits protein, qualified cDNA or exact GRCh37 remains usable.
    expected_protein = _mtbp_normalized_protein(_protein_change(variant.hgvsp))
    reported_proteins = _mtbp_proteins(text)
    if expected_protein and reported_proteins:
        return "protein" if expected_protein in reported_proteins else ""
    if _mtbp_row_cdna_matches(text, variant, require_transcript=bool(expected_protein)):
        return "cdna"
    return "genomic_grch37" if genomic_state is True else ""


def _review_query(variant: VariantRecord) -> str:
    return " ".join(
        value for value in [variant.symbol, variant.hgvsc, variant.hgvsp, variant.genomic_location]
        if value
    )
