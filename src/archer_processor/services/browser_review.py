from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from archer_processor.core.models import DatabaseEvidence, VariantRecord


BROWSER_DATABASES = ("OncoKB", "Franklin", "MTBP")

LOGIN_URLS = {
    "OncoKB": "https://www.oncokb.org/login",
    "Franklin": "https://franklin.genoox.com/login",
    "MTBP": "https://mtbp.org/analyse/",
}

_AMINO_ACID_3_TO_1 = {
    "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C",
    "Gln": "Q", "Glu": "E", "Gly": "G", "His": "H", "Ile": "I",
    "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F", "Pro": "P",
    "Ser": "S", "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V",
    "Ter": "*",
}


class BrowserAutomationUnavailable(RuntimeError):
    """The optional visible-browser runtime is not installed or cannot start."""


class BrowserReviewService:
    """Run serial, visible website reviews in isolated persistent Edge profiles.

    Passwords are entered directly into each provider's page. The application
    retains only the provider browser profile (cookies/local storage), never the
    password itself. Browser sources are deliberately serial and separate from
    the parallel HTTP/API search service.
    """

    def __init__(
        self,
        profile_root: Path | None = None,
        *,
        channel: str = "msedge",
        navigation_timeout_ms: int = 45_000,
        analysis_timeout_ms: int = 300_000,
        mtbp_cancer_type: str = "Blood",
    ) -> None:
        self.profile_root = profile_root or Path.home() / ".archer-prosess" / "browser_profiles"
        self.channel = channel
        self.navigation_timeout_ms = navigation_timeout_ms
        self.analysis_timeout_ms = analysis_timeout_ms
        self.mtbp_cancer_type = mtbp_cancer_type.strip() or "Blood"

    @staticmethod
    def dependency_available() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

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
        if database == "OncoKB":
            alteration = _protein_change(variant.hgvsp) or _cdna_change(variant.hgvsc)
            if not variant.symbol or not alteration:
                return ""
            return (
                "https://www.oncokb.org/gene/"
                f"{quote(variant.symbol, safe='')}/somatic/{quote(alteration, safe='')}"
            )
        if database == "Franklin":
            query = _franklin_query(variant)
            return (
                "https://franklin.genoox.com/clinical-db/variant/snp/"
                + quote(query, safe="")
                if query
                else ""
            )
        return LOGIN_URLS[database]

    def open_login(self, database: str, *, maximum_minutes: int = 30) -> str:
        """Open a visible login window and retain its session after it closes."""
        sync_playwright, playwright_error, _ = self._playwright_api()
        profile = self.profile_directory(database)
        with sync_playwright() as runtime:
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
                    "Confirm that Edge and the Python Playwright package are installed."
                ) from exc
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                self.login_url(database),
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            page.bring_to_front()
            deadline = datetime.now(timezone.utc).timestamp() + maximum_minutes * 60
            try:
                while context.pages and datetime.now(timezone.utc).timestamp() < deadline:
                    context.pages[0].wait_for_timeout(500)
            except playwright_error:
                pass
            finally:
                try:
                    context.close()
                except playwright_error:
                    pass
        return f"{database} browser session updated."

    def search_variants(
        self,
        variants: Iterable[VariantRecord],
        databases: Iterable[str],
        artifact_root: Path,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        variant_list = list(variants)
        database_list = [database for database in databases if database in BROWSER_DATABASES]
        results: dict[str, list[DatabaseEvidence]] = {
            self.variant_key(variant): [] for variant in variant_list
        }
        artifact_root.mkdir(parents=True, exist_ok=True)
        for database in database_list:
            if progress:
                progress(f"Browser review: starting {database}")
            database_results = self._search_database(
                database,
                variant_list,
                artifact_root / database.lower().replace(" ", "-"),
                progress=progress,
            )
            for key, evidence in database_results.items():
                results[key].append(evidence)
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

        sync_playwright, playwright_error, playwright_timeout = self._playwright_api()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        results: dict[str, DatabaseEvidence] = {}
        with sync_playwright() as runtime:
            try:
                context = runtime.chromium.launch_persistent_context(
                    str(self.profile_directory(database)),
                    channel=self.channel,
                    headless=False,
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 1000},
                )
            except Exception as exc:
                raise BrowserAutomationUnavailable(
                    "Could not start Microsoft Edge for browser review."
                ) from exc
            page = context.pages[0] if context.pages else context.new_page()
            try:
                for index, variant in enumerate(variants, start=1):
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
                        try:
                            page.wait_for_load_state("networkidle", timeout=12_000)
                        except playwright_timeout:
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
            finally:
                try:
                    context.close()
                except playwright_error:
                    pass
        return results

    def _search_mtbp(
        self,
        variants: list[VariantRecord],
        artifact_directory: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> dict[str, DatabaseEvidence]:
        """Submit one pseudonymous MTBP batch and map report rows back to variants."""
        results: dict[str, DatabaseEvidence] = {}
        query_pairs: list[tuple[VariantRecord, str]] = []
        for variant in variants:
            query = _mtbp_variant_query(variant)
            if query:
                query_pairs.append((variant, query))
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

        sync_playwright, playwright_error, playwright_timeout = self._playwright_api()
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
            with sync_playwright() as runtime:
                try:
                    context = runtime.chromium.launch_persistent_context(
                        str(self.profile_directory("MTBP")),
                        channel=self.channel,
                        headless=False,
                        accept_downloads=True,
                        viewport={"width": 1440, "height": 1000},
                    )
                except Exception as exc:
                    raise BrowserAutomationUnavailable(
                        "Could not start Microsoft Edge for MTBP browser review."
                    ) from exc
                page = context.pages[0] if context.pages else context.new_page()
                if progress:
                    progress(f"MTBP: submitting {len(submitted_queries)} pseudonymous variants")
                page.goto(
                    self.login_url("MTBP"),
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                if self._login_required("MTBP", page.url) or page.locator("#variant-input").count() != 1:
                    return {
                        **results,
                        **{
                            self.variant_key(variant): DatabaseEvidence(
                                "MTBP",
                                "login_required",
                                "Sign in to MTBP using the Browser Sign-in button, then retry.",
                                accession=query,
                                url=page.url,
                            )
                            for variant, query in query_pairs
                        },
                    }

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
                page.locator("#run-analysis").click()
                page.wait_for_url(
                    re.compile(r"https://mtbp\.org/(?:queue/\d+/?|patients/.+/report/\d+/?)"),
                    timeout=self.navigation_timeout_ms,
                )
                if "/queue/" in page.url:
                    if progress:
                        progress("MTBP: analysis queued; waiting for the report")
                    page.wait_for_url(
                        re.compile(r"https://mtbp\.org/patients/.+/report/\d+/?"),
                        timeout=self.analysis_timeout_ms,
                    )
                if progress:
                    progress("MTBP: report ready; validating returned variants")
                body_text = page.locator("body").inner_text(timeout=self.navigation_timeout_ms)
                version_tooltip = page.locator("[data-tooltip-html*='VEP:']")
                if version_tooltip.count():
                    version_html = version_tooltip.first.get_attribute("data-tooltip-html") or ""
                    body_text += "\n" + re.sub(r"<br\s*/?>", "\n", version_html, flags=re.IGNORECASE)
                report_rows = self._extract_mtbp_rows(page)
                screenshot_path = artifact_directory / f"{analysis_id.lower()}.png"
                page.screenshot(path=str(screenshot_path), full_page=False)
                parsed = parse_mtbp_report(
                    body_text,
                    report_rows,
                    [variant for variant, _ in query_pairs],
                    page.url,
                    cancer_type=self.mtbp_cancer_type,
                )
                captured_at = datetime.now(timezone.utc).isoformat()
                for variant, query in query_pairs:
                    key = self.variant_key(variant)
                    evidence = parsed[key]
                    evidence.accession = query
                    evidence.raw.update(
                        {
                            "analysis_id": analysis_id,
                            "submitted_query": query,
                            "captured_at": captured_at,
                            "screenshot": str(screenshot_path),
                            "visible_text_preview": body_text[:12_000],
                        }
                    )
                    audit_path = self._screenshot_path(
                        artifact_directory, "MTBP", variant
                    ).with_suffix(".audit.json")
                    audit_path.write_text(
                        json.dumps(asdict(evidence), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    results[key] = evidence
        except playwright_timeout:
            current_url = context.pages[0].url if context and context.pages else self.login_url("MTBP")
            for variant, query in query_pairs:
                results[self.variant_key(variant)] = DatabaseEvidence(
                    "MTBP",
                    "timeout",
                    "MTBP did not produce a report before the five-minute timeout.",
                    accession=query,
                    url=current_url,
                )
        except Exception as exc:
            for variant, query in query_pairs:
                results[self.variant_key(variant)] = DatabaseEvidence(
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
                except playwright_error:
                    pass
        return results

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
                  return {
                    section,
                    gene: cells[0]?.innerText.trim() || '',
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
        current_url = page.url
        if self._login_required(database, current_url):
            return DatabaseEvidence(
                database=database,
                status="login_required",
                summary=f"Sign in to {database} using the Browser Sign-in button, then retry.",
                accession=_review_query(variant),
                url=current_url,
            )
        body_text = page.locator("body").inner_text(timeout=self.navigation_timeout_ms)
        screenshot_path = self._screenshot_path(artifact_directory, database, variant)
        page.screenshot(path=str(screenshot_path), full_page=False)
        if database == "OncoKB":
            evidence = parse_oncokb_page(body_text, variant, current_url)
        else:
            evidence = parse_franklin_page(body_text, variant, current_url)
        evidence.raw.update(
            {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "screenshot": str(screenshot_path),
                "visible_text_preview": body_text[:12_000],
            }
        )
        audit_path = screenshot_path.with_suffix(".audit.json")
        audit_path.write_text(
            json.dumps(asdict(evidence), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return evidence

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
        if database == "OncoKB":
            return "/login" in lowered
        if database == "Franklin":
            return "/login" in lowered or "auth0" in lowered
        if database == "MTBP":
            return "keycloak" in lowered or "/auth/" in lowered
        return "login" in lowered or "signin" in lowered

    def _playwright_api(self):
        try:
            from playwright.sync_api import (
                Error as PlaywrightError,
                TimeoutError as PlaywrightTimeoutError,
                sync_playwright,
            )
        except ImportError as exc:
            raise BrowserAutomationUnavailable(
                "Browser review requires Playwright. Reinstall Archer Prosess packages."
            ) from exc
        return sync_playwright, PlaywrightError, PlaywrightTimeoutError

    def _validate_database(self, database: str) -> None:
        if database not in BROWSER_DATABASES:
            raise ValueError(f"Unsupported browser database: {database}")

    @staticmethod
    def variant_key(variant: VariantRecord) -> str:
        return f"{variant.sample}|{variant.hgvsc}"


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
    classification = _after_heading(body_text, "Suggested classification")
    if not classification:
        return DatabaseEvidence(
            "Franklin", "not_found", f"No Franklin web result for {_franklin_query(variant)}.",
            accession=_franklin_query(variant), url=url,
        )
    rule_pattern = re.compile(
        r"\b(?:PVS1|PS[1-4]|PM[1-6]|PP[1-5]|BA1|BS[1-4]|BP[1-7])\b"
    )
    rules = list(dict.fromkeys(rule_pattern.findall(body_text)))
    parts = [f"classification={classification}"]
    if rules:
        parts.append("displayed_ACMG_rules=" + ",".join(rules))
    return DatabaseEvidence(
        database="Franklin",
        status="found",
        summary="; ".join(parts),
        accession=_franklin_query(variant),
        clinical_significance=classification,
        url=url,
        raw={"classification": classification, "displayed_acmg_rules": rules},
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
    the report contains one row for that gene; ambiguous rows fail closed.
    """
    metadata = _mtbp_report_metadata(body_text, cancer_type)
    results: dict[str, DatabaseEvidence] = {}
    for variant in variants:
        key = BrowserReviewService.variant_key(variant)
        candidates = [
            row for row in report_rows
            if str(row.get("gene", "")).casefold() == variant.symbol.casefold()
        ]
        expected_protein = _mtbp_normalized_protein(_protein_change(variant.hgvsp))
        expected_cdna = _cdna_change(variant.hgvsc).casefold()
        if expected_protein:
            protein_matches = [
                row for row in candidates
                if expected_protein in _mtbp_proteins(str(row.get("identity_text", "")))
                or expected_protein in _mtbp_proteins(str(row.get("alteration", "")))
            ]
            candidates = protein_matches
        elif expected_cdna:
            cdna_matches = [
                row for row in candidates
                if expected_cdna in re.sub(r"\s+", "", str(row.get("identity_text", ""))).casefold()
            ]
            candidates = cdna_matches

        if len(candidates) != 1:
            status = "not_found" if not candidates else "ambiguous_result"
            detail = "no matching row" if not candidates else f"{len(candidates)} possible rows"
            results[key] = DatabaseEvidence(
                "MTBP",
                status,
                f"MTBP report returned {detail} for {_mtbp_variant_query(variant)}.",
                accession=_mtbp_variant_query(variant),
                url=url,
                raw={**metadata, "candidate_count": len(candidates)},
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


def _franklin_query(variant: VariantRecord) -> str:
    if variant.genomic_location and variant.ref_allele and variant.alt_allele:
        try:
            chromosome, position = variant.genomic_location.split(":", 1)
            chromosome = chromosome if chromosome.lower().startswith("chr") else f"chr{chromosome}"
            return f"{chromosome}-{position.split('-', 1)[0]}-{variant.ref_allele}-{variant.alt_allele}"
        except ValueError:
            pass
    if variant.symbol and _cdna_change(variant.hgvsc):
        return f"{variant.symbol}:{_cdna_change(variant.hgvsc)}"
    return variant.hgvsc or variant.genomic_location


def _mtbp_variant_query(variant: VariantRecord) -> str:
    protein = _protein_change(variant.hgvsp)
    if variant.symbol and protein:
        return f"{variant.symbol}:p.{protein}"
    cdna = _cdna_change(variant.hgvsc)
    if variant.symbol and cdna:
        return f"{variant.symbol}:{cdna}"
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


def _mtbp_normalized_protein(value: str) -> str:
    clean = (value or "").replace("p.", "").replace("(", "").replace(")", "").strip()
    match = re.fullmatch(r"([A-Z][a-z]{2})(\d+)([A-Z][a-z]{2}|Ter|\*)", clean)
    if match:
        reference, position, alternate = match.groups()
        reference = _AMINO_ACID_3_TO_1.get(reference, reference)
        alternate = "*" if alternate in {"Ter", "*"} else _AMINO_ACID_3_TO_1.get(alternate, alternate)
        clean = f"{reference}{position}{alternate}"
    return clean.casefold()


def _mtbp_proteins(text: str) -> set[str]:
    tokens = re.findall(
        r"p\.([A-Z][a-z]{2}\d+(?:[A-Z][a-z]{2}|Ter|\*)|[A-Z*]\d+[A-Z*])",
        text or "",
    )
    return {_mtbp_normalized_protein(token) for token in tokens}


def _review_query(variant: VariantRecord) -> str:
    return " ".join(
        value for value in [variant.symbol, variant.hgvsc, variant.hgvsp, variant.genomic_location]
        if value
    )
