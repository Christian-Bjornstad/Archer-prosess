from __future__ import annotations

import html
import os
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import requests

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.settings import AppSettings


MANUAL_DATABASES = {
    "MTBP": "Manual/licensed review. Record: classification, functional relevance/evidence category, population AF, ClinVar class, references, notes.",
    "HSMD": "Manual/licensed review. Record: classification, actionability tier, clinical review status, population frequency, references, notes.",
}


class DatabaseSearchService:
    _gnomad_lock = threading.Lock()
    _last_gnomad_request = 0.0

    def __init__(self, settings: AppSettings | None = None, timeout: int = 12) -> None:
        self.settings = settings or AppSettings.load()
        self.timeout = timeout
        self._gnomad_cache: dict[str, DatabaseEvidence] = {}
        self._oncokb_info_cache: dict | None = None

    def database_diagnostics(self, databases: Iterable[str]) -> dict[str, str]:
        diagnostics = {}
        for database in databases:
            if database in MANUAL_DATABASES:
                diagnostics[database] = "manual"
            elif database == "OncoKB" and not self.settings.oncokb_api_key:
                diagnostics[database] = "token required"
            elif database == "Franklin" and self.settings.franklin_api_key:
                diagnostics[database] = "ready"
            elif database == "Franklin" and self._has_franklin_login_credentials():
                diagnostics[database] = "ready (login on search)"
            elif database == "Franklin":
                diagnostics[database] = "token required"
            elif database == "gnomAD":
                diagnostics[database] = f"ready ({self.settings.gnomad_dataset}, rate limited)"
            elif database == "COSMIC":
                diagnostics[database] = "ready (basic/public lookup)"
            elif database in {"ClinVar", "OncoKB"}:
                diagnostics[database] = "ready"
            else:
                diagnostics[database] = "manual"
        return diagnostics

    def search_variant(self, variant: VariantRecord, databases: Iterable[str]) -> list[DatabaseEvidence]:
        evidence: list[DatabaseEvidence] = []
        for database in databases:
            if database == "ClinVar":
                evidence.append(self._search_clinvar(variant))
            elif database == "COSMIC":
                evidence.append(self._search_cosmic(variant))
            elif database == "OncoKB":
                evidence.append(self._search_oncokb(variant))
            elif database == "Franklin":
                evidence.append(self._search_franklin(variant))
            elif database == "gnomAD":
                evidence.append(self._search_gnomad(variant))
            else:
                evidence.append(self._manual_evidence(database, variant))
        return evidence

    def search_variants_parallel(
        self,
        variants: list[VariantRecord],
        databases: Iterable[str],
        max_workers: int = 3,
        progress: Callable[[int, int, VariantRecord], None] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        database_list = list(databases)
        total = len(variants)
        if total == 0:
            return {}

        workers = max(1, min(int(max_workers or 1), 8, total))
        results: dict[str, list[DatabaseEvidence]] = {}
        completed = 0

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="db-search") as executor:
            future_map = {
                executor.submit(self.search_variant, variant, database_list): variant
                for variant in variants
            }
            for future in as_completed(future_map):
                variant = future_map[future]
                key = self.variant_key(variant)
                try:
                    results[key] = future.result()
                except Exception as exc:
                    results[key] = [
                        DatabaseEvidence(
                            database="Search",
                            status="error",
                            summary=f"Parallel search failed for {variant.display_name}: {exc}",
                        )
                    ]
                completed += 1
                if progress:
                    progress(completed, total, variant)
        return results

    def variant_key(self, variant: VariantRecord) -> str:
        return f"{variant.sample}|{variant.hgvsc}"

    def _manual_evidence(self, database: str, variant: VariantRecord) -> DatabaseEvidence:
        query = self._review_query(variant)
        return DatabaseEvidence(
            database=database,
            status="manual",
            summary=f"{MANUAL_DATABASES.get(database, 'Manual database check required')} Query: {query}",
            accession=query,
            url=self._manual_url(database, variant),
            raw={"query": query, "checklist": MANUAL_DATABASES.get(database, "Manual database check required")},
        )

    def _search_clinvar(self, variant: VariantRecord) -> DatabaseEvidence:
        query = variant.hgvsc
        if not query:
            return DatabaseEvidence("ClinVar", "invalid_query", "Missing HGVSc.")
        try:
            clinvar_id = ""
            used_query = query
            for candidate in self._clinvar_queries(variant):
                params = {
                    "db": "clinvar",
                    "term": candidate,
                    "retmode": "xml",
                    "retmax": "1",
                }
                if self.settings.clinvar_api_key:
                    params["api_key"] = self.settings.clinvar_api_key
                search = requests.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params=params,
                    timeout=self.timeout,
                )
                search.raise_for_status()
                root = ET.fromstring(search.content)
                clinvar_id = root.findtext(".//Id") or ""
                if clinvar_id:
                    used_query = candidate
                    break
            if not clinvar_id:
                return DatabaseEvidence(
                    database="ClinVar",
                    status="not_found",
                    summary=f"No ClinVar result for {query}.",
                    url=self._clinvar_search_url(query),
                )
            fetch_params = {
                "db": "clinvar",
                "id": clinvar_id,
                "retmode": "xml",
                "rettype": "vcv",
                "is_variationid": "true",
            }
            if self.settings.clinvar_api_key:
                fetch_params["api_key"] = self.settings.clinvar_api_key
            fetch = requests.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params=fetch_params,
                timeout=self.timeout,
            )
            fetch.raise_for_status()
            return self._parse_clinvar(fetch.content, used_query, clinvar_id)
        except Exception as exc:
            return DatabaseEvidence(
                database="ClinVar",
                status="error",
                summary=f"ClinVar lookup failed: {exc}",
                url=self._clinvar_search_url(query),
            )

    def _parse_clinvar(self, content: bytes, query: str, clinvar_id: str) -> DatabaseEvidence:
        text = content.decode("utf-8", errors="replace")
        root = ET.fromstring(content)
        archive = root.find(".//VariationArchive")
        title = ""
        accession = ""
        if archive is not None:
            title = archive.attrib.get("VariationName", "")
            accession = archive.attrib.get("Accession", "")
            version = archive.attrib.get("Version", "")
            if accession and version:
                accession = f"{accession}.{version}"
        title = title or root.findtext(".//Title") or root.findtext(".//Name/ElementValue") or query
        significance = (
            root.findtext(".//ClinicalSignificance/Description")
            or root.findtext(".//GermlineClassification/Description")
            or ""
        )
        review_status = (
            root.findtext(".//ClinicalSignificance/ReviewStatus")
            or root.findtext(".//GermlineClassification/ReviewStatus")
            or ""
        )
        accession_node = root.find(".//*[@Accession]")
        if not accession and accession_node is not None:
            accession = accession_node.attrib.get("Accession", "")
            version = accession_node.attrib.get("Version", "")
            if version:
                accession = f"{accession}.{version}"
        clean_title = html.unescape(title)
        parts = [clean_title]
        if significance:
            parts.append(f"Significance: {significance}")
        if review_status:
            parts.append(f"Review: {review_status}")
        return DatabaseEvidence(
            database="ClinVar",
            status="found",
            summary=" | ".join(parts),
            accession=accession or clinvar_id,
            clinical_significance=significance,
            url=f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{clinvar_id}/",
            raw={"query": query, "clinvar_id": clinvar_id, "xml_preview": text[:1000]},
        )

    def _clinvar_search_url(self, query: str) -> str:
        return "https://www.ncbi.nlm.nih.gov/clinvar/?term=" + urllib.parse.quote(query)

    def _clinvar_queries(self, variant: VariantRecord) -> list[str]:
        cdna = self._cdna_without_transcript(variant.hgvsc)
        protein = self._protein_change(variant.hgvsp)
        queries = [
            f"{variant.hgvsc}[VARNAME]",
        ]
        if variant.symbol and protein:
            queries.append(f"{variant.symbol} {protein}")
        if variant.symbol and cdna:
            queries.append(f"{variant.symbol} {cdna}")
        queries.append(variant.hgvsc)
        if variant.genomic_location:
            queries.append(variant.genomic_location)
        unique = []
        for query in queries:
            if query and query not in unique:
                unique.append(query)
        return unique

    def _search_cosmic(self, variant: VariantRecord) -> DatabaseEvidence:
        queries = self._cosmic_queries(variant)
        if not queries:
            return DatabaseEvidence("COSMIC", "invalid_query", "Missing gene/HGVS fields.")
        fields = [
            "MutationID",
            "LegacyMutationID",
            "GenomicMutationID",
            "GeneName",
            "MutationCDS",
            "MutationAA",
            "MutationDescription",
            "MutationGenomePosition",
            "GRChVer",
            "PrimarySite",
            "PrimaryHistology",
            "PubmedPMID",
        ]
        try:
            last_data = None
            for cosmic_query in queries:
                params = {
                    "terms": cosmic_query["terms"],
                    "maxList": 8,
                    "grchv": "37",
                    "ef": ",".join(fields),
                }
                if cosmic_query.get("q"):
                    params["q"] = cosmic_query["q"]
                response = requests.get(
                    "https://clinicaltables.nlm.nih.gov/api/cosmic/v4/search",
                    params=params,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                last_data = data
                total = int(data[0]) if data else 0
                if total:
                    return self._format_cosmic_evidence(variant, cosmic_query["label"], data)
            return DatabaseEvidence(
                "COSMIC",
                "not_found",
                f"No COSMIC basic/public hit for {'; '.join(query['label'] for query in queries)}.",
                url=self._manual_url("COSMIC", variant),
                raw={"queries": queries, "last_response": last_data},
            )
        except Exception as exc:
            return DatabaseEvidence("COSMIC", "error", f"COSMIC lookup failed: {exc}", url=self._manual_url("COSMIC", variant))

    def _format_cosmic_evidence(self, variant: VariantRecord, query: str, data: list) -> DatabaseEvidence:
        total = int(data[0]) if data else 0
        extras = data[2] or {}
        ids = extras.get("MutationID", []) or data[1] or []
        legacy_ids = extras.get("LegacyMutationID", [])
        genes = extras.get("GeneName", [])
        cds = extras.get("MutationCDS", [])
        aa = extras.get("MutationAA", [])
        descriptions = extras.get("MutationDescription", [])
        positions = extras.get("MutationGenomePosition", [])
        grch = extras.get("GRChVer", [])
        sites = extras.get("PrimarySite", [])
        first = []
        for index, mutation_id in enumerate(ids[:3]):
            bits = [
                mutation_id,
                self._at(legacy_ids, index),
                self._at(genes, index),
                self._at(cds, index),
                self._at(aa, index),
                self._at(descriptions, index),
                self._at(positions, index),
                self._at(grch, index),
                self._at(sites, index),
            ]
            first.append(" ".join(bit for bit in bits if bit))
        summary = f"COSMIC basic/public lookup: {total} match(es) for {query}. " + "; ".join(first)
        return DatabaseEvidence(
            database="COSMIC",
            status="found",
            summary=summary.strip(),
            accession=", ".join(self._unique_text(ids)[:5]),
            url=self._manual_url("COSMIC", variant),
            raw={"query": query, "extra": extras},
        )

    def _search_oncokb(self, variant: VariantRecord) -> DatabaseEvidence:
        alteration = self._protein_change(variant.hgvsp) or self._cdna_without_transcript(variant.hgvsc)
        if not variant.symbol or not alteration:
            return DatabaseEvidence("OncoKB", "invalid_query", "Needs gene plus HGVSp or HGVSc.", url=self._manual_url("OncoKB", variant))
        if not self.settings.oncokb_api_key:
            return DatabaseEvidence(
                database="OncoKB",
                status="token_required",
                summary=f"OncoKB API requires a token. Query prepared: {variant.symbol} {alteration}.",
                accession=f"{variant.symbol} {alteration}",
                url=self._manual_url("OncoKB", variant),
            )
        try:
            info = self._oncokb_info()
            response = requests.get(
                "https://www.oncokb.org/api/v1/annotate/mutations/byProteinChange",
                params={"hugoSymbol": variant.symbol, "alteration": alteration},
                headers={"Authorization": f"Bearer {self.settings.oncokb_api_key}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            summary_parts = [
                f"oncogenic={data.get('oncogenic', '')}",
                f"mutation_effect={data.get('mutationEffect', {}).get('knownEffect', '')}",
                f"highest_sensitive_level={data.get('highestSensitiveLevel', '')}",
                f"highest_resistance_level={data.get('highestResistanceLevel', '')}",
                f"highest_diagnostic_level={data.get('highestDiagnosticImplicationLevel', '')}",
                f"highest_prognostic_level={data.get('highestPrognosticImplicationLevel', '')}",
                f"data_version={self._oncokb_info_value(info)}",
            ]
            return DatabaseEvidence(
                database="OncoKB",
                status="found",
                summary="; ".join(part for part in summary_parts if not part.endswith("=")),
                accession=f"{variant.symbol} {alteration}",
                clinical_significance=data.get("oncogenic", ""),
                url=self._manual_url("OncoKB", variant),
                raw={"info": info, "annotation": data},
            )
        except requests.HTTPError as exc:
            status = "unauthorized" if exc.response is not None and exc.response.status_code in {401, 403} else "error"
            return DatabaseEvidence("OncoKB", status, f"OncoKB lookup failed: {exc}", url=self._manual_url("OncoKB", variant))
        except Exception as exc:
            return DatabaseEvidence("OncoKB", "error", f"OncoKB lookup failed: {exc}", url=self._manual_url("OncoKB", variant))

    def _search_franklin(self, variant: VariantRecord) -> DatabaseEvidence:
        query = self._franklin_search_text(variant)
        if not query:
            return DatabaseEvidence("Franklin", "invalid_query", "Needs genomic position/ref/alt, HGVSc, or gene alteration.", url=self._manual_url("Franklin", variant))
        token = self._franklin_token()
        if not token:
            return DatabaseEvidence(
                database="Franklin",
                status="token_required",
                summary=f"Franklin API requires a token or runtime email/password login. Query prepared: {query}.",
                accession=query,
                url=self._manual_url("Franklin", variant),
            )
        try:
            response = requests.get(
                "https://api.genoox.com/v2/search/snp/",
                params={"search_text": query},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return self._format_franklin_evidence(variant, query, data)
        except requests.HTTPError as exc:
            status = "unauthorized" if exc.response is not None and exc.response.status_code in {401, 403} else "error"
            return DatabaseEvidence("Franklin", status, f"Franklin lookup failed: {exc}", url=self._manual_url("Franklin", variant))
        except Exception as exc:
            return DatabaseEvidence("Franklin", "error", f"Franklin lookup failed: {exc}", url=self._manual_url("Franklin", variant))

    def _franklin_token(self) -> str:
        if self.settings.franklin_api_key:
            return self.settings.franklin_api_key
        email = self.settings.franklin_email or os.environ.get("FRANKLIN_EMAIL", "")
        password = self.settings.franklin_password or os.environ.get("FRANKLIN_PASSWORD", "")
        if not email or not password:
            return ""
        response = requests.get(
            "https://api.genoox.com/v1/auth/login",
            params={"email": email},
            headers={"Authorization": password, "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        token = self._token_from_payload(response.json())
        if not token:
            raise ValueError("Franklin login did not return an API token.")
        self.settings.franklin_api_key = token
        return token

    def _has_franklin_login_credentials(self) -> bool:
        email = self.settings.franklin_email or os.environ.get("FRANKLIN_EMAIL", "")
        password = self.settings.franklin_password or os.environ.get("FRANKLIN_PASSWORD", "")
        return bool(email and password)

    def _token_from_payload(self, payload) -> str:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return ""
        for key in ["token", "api_token", "apiToken", "user_api_token", "userApiToken", "access_token", "accessToken"]:
            if payload.get(key):
                return str(payload[key])
        for key in ["data", "user", "result"]:
            token = self._token_from_payload(payload.get(key))
            if token:
                return token
        return ""

    def _format_franklin_evidence(self, variant: VariantRecord, query: str, data: dict) -> DatabaseEvidence:
        record = self._first_franklin_record(data)
        if not record:
            return DatabaseEvidence(
                database="Franklin",
                status="not_found",
                summary=f"No Franklin result for {query}.",
                url=self._manual_url("Franklin", variant),
                raw=data,
            )

        annotations = record.get("annotations") or {}
        classification = record.get("classification") or {}
        clinical = annotations.get("clinical_evidences") or record.get("clinical_evidences") or {}
        predictions = annotations.get("predictions") or {}
        frequencies = annotations.get("frequencies") or {}
        transcripts = annotations.get("transcripts") or []

        acmg = str(classification.get("acmg_classification") or classification.get("computed_classification") or "")
        acmg_rules = self._franklin_met_rules(classification)
        frequency_text = self._franklin_frequency_text(frequencies)
        prediction_text = self._franklin_prediction_text(predictions)
        clinvar_text = self._franklin_clinvar_text(clinical)
        transcript_text = self._franklin_transcript_text(transcripts)

        parts = [f"query={query}"]
        if acmg:
            parts.append(f"ACMG={acmg}")
        if acmg_rules:
            parts.append(f"rules={acmg_rules}")
        if frequency_text:
            parts.append(frequency_text)
        if prediction_text:
            parts.append(prediction_text)
        if clinvar_text:
            parts.append(f"ClinVar={clinvar_text}")
        if transcript_text:
            parts.append(f"transcript={transcript_text}")

        return DatabaseEvidence(
            database="Franklin",
            status="found",
            summary="; ".join(parts),
            accession=query,
            clinical_significance=acmg,
            url=self._franklin_url(record, variant),
            raw=data,
        )

    def _search_gnomad(self, variant: VariantRecord) -> DatabaseEvidence:
        variant_id = self._gnomad_variant_id(variant)
        if not variant_id:
            return DatabaseEvidence(
                database="gnomAD",
                status="unsupported_query",
                summary="gnomAD live lookup needs genomic location plus ref/alt alleles.",
                url=self._manual_url("gnomAD", variant),
            )
        cached = self._gnomad_cache.get(variant_id)
        if cached:
            return cached
        query = """
        query Variant($variantId: String!, $datasetId: DatasetId!) {
          variant(variantId: $variantId, dataset: $datasetId) {
            variant_id
            reference_genome
            rsids
            exome {
              ac an af ac_hom ac_hemi homozygote_count hemizygote_count filters
              populations { id ac an ac_hom ac_hemi homozygote_count hemizygote_count }
              faf95 { popmax popmax_population }
            }
            genome {
              ac an af ac_hom ac_hemi homozygote_count hemizygote_count filters
              populations { id ac an ac_hom ac_hemi homozygote_count hemizygote_count }
              faf95 { popmax popmax_population }
            }
          }
        }
        """
        try:
            self._wait_for_gnomad_slot()
            response = requests.post(
                "https://gnomad.broadinstitute.org/api/",
                json={"query": query, "variables": {"variantId": variant_id, "datasetId": self.settings.gnomad_dataset}},
                timeout=self.timeout,
            )
            if response.status_code == 429:
                evidence = DatabaseEvidence(
                    "gnomAD",
                    "rate_limited",
                    "gnomAD rate limit reached: 10 requests per IP per 60 seconds. Try again later or lower Workers to 1.",
                    accession=variant_id,
                    url=self._manual_url("gnomAD", variant),
                )
                self._gnomad_cache[variant_id] = evidence
                return evidence
            response.raise_for_status()
            payload = response.json()
            graphql_errors = payload.get("errors") or []
            data = (payload.get("data") or {}).get("variant")
            if not data:
                message = graphql_errors[0].get("message") if graphql_errors and isinstance(graphql_errors[0], dict) else ""
                evidence = DatabaseEvidence(
                    "gnomAD",
                    "not_found",
                    f"No {self.settings.gnomad_dataset} hit for {variant_id}." + (f" API message: {message}" if message else ""),
                    accession=variant_id,
                    url=self._manual_url("gnomAD", variant),
                    raw=payload,
                )
                self._gnomad_cache[variant_id] = evidence
                return evidence
            evidence = self._format_gnomad_evidence(variant, variant_id, data)
            self._gnomad_cache[variant_id] = evidence
            return evidence
        except Exception as exc:
            return DatabaseEvidence("gnomAD", "error", f"gnomAD lookup failed: {exc}", url=self._manual_url("gnomAD", variant))

    def _format_gnomad_evidence(self, variant: VariantRecord, variant_id: str, data: dict) -> DatabaseEvidence:
        exome = data.get("exome") or {}
        genome = data.get("genome") or {}
        exome_af = self._frequency(exome)
        genome_af = self._frequency(genome)
        aggregated_ac = (exome.get("ac") or 0) + (genome.get("ac") or 0)
        aggregated_an = (exome.get("an") or 0) + (genome.get("an") or 0)
        aggregated_af = aggregated_ac / aggregated_an if aggregated_an else None
        max_source, max_population, max_af = self._max_population_frequency(exome, genome)
        hom_count = (exome.get("homozygote_count") or exome.get("ac_hom") or 0) + (genome.get("homozygote_count") or genome.get("ac_hom") or 0)
        hemi_count = (exome.get("hemizygote_count") or exome.get("ac_hemi") or 0) + (genome.get("hemizygote_count") or genome.get("ac_hemi") or 0)
        filters = sorted({str(item) for item in (exome.get("filters") or []) + (genome.get("filters") or [])})

        parts = [
            f"dataset={self.settings.gnomad_dataset}",
            f"variant={variant_id}",
        ]
        if aggregated_af is not None:
            parts.append(f"aggregated_AF={self._pct(aggregated_af)} ({aggregated_ac}/{aggregated_an})")
        if exome_af is not None:
            parts.append(f"exome_AF={self._pct(exome_af)} ({exome.get('ac')}/{exome.get('an')})")
        if genome_af is not None:
            parts.append(f"genome_AF={self._pct(genome_af)} ({genome.get('ac')}/{genome.get('an')})")
        if max_af is not None:
            parts.append(f"max_population_AF={self._pct(max_af)} ({max_source}:{max_population})")
        parts.append(f"homozygotes={hom_count}")
        if hemi_count:
            parts.append(f"hemizygotes={hemi_count}")
        if filters:
            parts.append("filters=" + ",".join(filters))
        if max_af is not None:
            parts.append("frequency_context=" + self._frequency_context(max_af, hom_count))

        return DatabaseEvidence(
                database="gnomAD",
                status="found",
                summary="; ".join(parts),
                accession=", ".join(data.get("rsids") or []),
                url=self._manual_url("gnomAD", variant),
                raw=data,
            )

    def _oncokb_info(self) -> dict:
        if self._oncokb_info_cache is not None:
            return self._oncokb_info_cache
        response = requests.get(
            "https://www.oncokb.org/api/v1/info",
            headers={"Authorization": f"Bearer {self.settings.oncokb_api_key}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        self._oncokb_info_cache = response.json()
        return self._oncokb_info_cache

    def _oncokb_info_value(self, info: dict) -> str:
        for key in ["dataVersion", "data_version", "version", "apiVersion"]:
            if info.get(key):
                return str(info[key])
        return ""

    def _wait_for_gnomad_slot(self) -> None:
        with self._gnomad_lock:
            elapsed = time.monotonic() - self._last_gnomad_request
            wait_seconds = max(0.0, 6.2 - elapsed)
            if wait_seconds:
                time.sleep(wait_seconds)
            type(self)._last_gnomad_request = time.monotonic()

    def _frequency(self, source: dict) -> float | None:
        if source.get("af") is not None:
            return source["af"]
        ac = source.get("ac")
        an = source.get("an")
        return ac / an if ac is not None and an else None

    def _max_population_frequency(self, *sources: dict) -> tuple[str, str, float | None]:
        best_source = ""
        best_population = ""
        best_af: float | None = None
        for source_name, source in zip(["exome", "genome"], sources):
            faf95 = source.get("faf95") or {}
            if faf95.get("popmax") is not None and (best_af is None or faf95["popmax"] > best_af):
                best_source = source_name
                best_population = faf95.get("popmax_population") or "faf95_popmax"
                best_af = faf95["popmax"]
            for population in source.get("populations") or []:
                af = self._frequency(population)
                if af is not None and (best_af is None or af > best_af):
                    best_source = source_name
                    best_population = population.get("id") or "population"
                    best_af = af
        return best_source, best_population, best_af

    def _pct(self, value: float) -> str:
        return f"{value:.4%}"

    def _frequency_context(self, max_af: float, hom_count: int) -> str:
        if max_af >= 0.01:
            return "common_population_variant"
        if max_af >= 0.001:
            return "low_frequency_population_variant"
        if hom_count:
            return "very_rare_with_homozygotes"
        return "very_rare_or_absent"

    def _gnomad_variant_id(self, variant: VariantRecord) -> str:
        if not variant.genomic_location or not variant.ref_allele or not variant.alt_allele:
            return ""
        try:
            chrom, pos = variant.genomic_location.replace("chr", "").split(":", 1)
            pos = pos.split("-", 1)[0]
            return f"{chrom}-{pos}-{variant.ref_allele}-{variant.alt_allele}"
        except ValueError:
            return ""

    def _cosmic_queries(self, variant: VariantRecord) -> list[dict[str, str]]:
        queries = []
        if variant.cosmic_id:
            digits = "".join(char for char in variant.cosmic_id if char.isdigit())
            exact_parts = [f'LegacyMutationID:"{variant.cosmic_id}"']
            if digits:
                exact_parts.append(f'MutationID:"{digits}"')
            queries.append(
                {
                    "terms": variant.symbol or variant.cosmic_id,
                    "q": " OR ".join(exact_parts),
                    "label": variant.cosmic_id,
                }
            )
        cdna = self._cdna_without_transcript(variant.hgvsc)
        protein = self._protein_change(variant.hgvsp)
        for query in [
            " ".join(part for part in [variant.symbol, cdna] if part),
            " ".join(part for part in [variant.symbol, protein] if part),
            " ".join(part for part in [variant.symbol, variant.genomic_location] if part),
            variant.symbol,
        ]:
            if query and query not in [item["label"] for item in queries]:
                queries.append({"terms": query, "label": query})
        return queries

    def _franklin_search_text(self, variant: VariantRecord) -> str:
        if variant.genomic_location and variant.ref_allele and variant.alt_allele:
            try:
                chrom, pos = variant.genomic_location.split(":", 1)
                pos = pos.split("-", 1)[0]
                if not chrom.lower().startswith("chr"):
                    chrom = f"chr{chrom}"
                return f"{chrom}-{pos}-{variant.ref_allele}-{variant.alt_allele}"
            except ValueError:
                pass
        cdna = self._cdna_without_transcript(variant.hgvsc)
        if variant.symbol and cdna:
            return f"{variant.symbol}:{cdna}"
        return variant.hgvsc or variant.genomic_location or variant.display_name

    def _first_franklin_record(self, data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        for key in ["variant", "result"]:
            if isinstance(data.get(key), dict):
                return data[key]
        for key in ["variants", "results", "data"]:
            value = data.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
            if isinstance(value, dict):
                nested = self._first_franklin_record(value)
                if nested:
                    return nested
        return data if any(key in data for key in ["annotations", "classification", "clinical_evidences"]) else {}

    def _franklin_met_rules(self, classification: dict) -> str:
        rules = []
        for rule in classification.get("acmg_rules") or classification.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            is_met = rule.get("is_met")
            if is_met is False:
                continue
            name = rule.get("name") or rule.get("code") or rule.get("rule")
            if name:
                rules.append(str(name))
        return ",".join(rules[:8])

    def _franklin_frequency_text(self, frequencies: dict | list) -> str:
        entries = []
        if isinstance(frequencies, dict):
            iterable = frequencies.values()
        elif isinstance(frequencies, list):
            iterable = frequencies
        else:
            iterable = []
        for item in iterable:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or item.get("db") or item.get("database") or item.get("name")
            value = item.get("frequency") or item.get("af") or item.get("allele_frequency")
            population = item.get("population") or item.get("subpopulation")
            if value is not None:
                label = str(source or "frequency")
                if population:
                    label = f"{label}:{population}"
                entries.append(f"{label}={value}")
        return "frequencies=" + ", ".join(entries[:5]) if entries else ""

    def _franklin_prediction_text(self, predictions: dict) -> str:
        if not isinstance(predictions, dict):
            return ""
        wanted = ["revel", "aggregated_predictions", "sift", "polyphen", "splice_ai"]
        entries = []
        for key in wanted:
            value = predictions.get(key)
            if value in [None, "", {}]:
                continue
            if isinstance(value, dict):
                score = value.get("score") or value.get("prediction") or value.get("value")
                value = score if score not in [None, ""] else value
            entries.append(f"{key}={value}")
        return "predictions=" + ", ".join(entries[:5]) if entries else ""

    def _franklin_clinvar_text(self, clinical: dict) -> str:
        clinvar = clinical.get("clinvar") if isinstance(clinical, dict) else None
        if not isinstance(clinvar, dict):
            return ""
        classification = clinvar.get("classification") or clinvar.get("clinical_significance")
        submissions = clinvar.get("submissions_by_classification") or []
        if submissions and isinstance(submissions, list):
            counts = []
            for item in submissions:
                if isinstance(item, dict):
                    label = item.get("classification") or item.get("clinical_significance") or item.get("name")
                    count = item.get("count")
                    if label:
                        counts.append(f"{label}:{count}" if count is not None else str(label))
            return ", ".join(counts[:5])
        return str(classification or "")

    def _franklin_transcript_text(self, transcripts: list) -> str:
        if not isinstance(transcripts, list) or not transcripts:
            return ""
        first = next((item for item in transcripts if isinstance(item, dict) and item.get("transcript_type") == "REFSEQ"), transcripts[0])
        if not isinstance(first, dict):
            return ""
        return " ".join(str(first.get(key)) for key in ["transcript", "cdot", "pdot"] if first.get(key))

    def _franklin_url(self, record: dict, variant: VariantRecord) -> str:
        for key in ["link", "url", "franklin_url", "variant_url"]:
            value = record.get(key)
            if value:
                return str(value)
        return self._manual_url("Franklin", variant)

    def _manual_url(self, database: str, variant: VariantRecord) -> str:
        query = urllib.parse.quote(self._review_query(variant))
        urls = {
            "MTBP": "https://mtbp.herokuapp.com/",
            "HSMD": "https://variants.ingenuity.com/",
            "COSMIC": f"https://cancer.sanger.ac.uk/cosmic/search?q={query}",
            "OncoKB": f"https://www.oncokb.org/gene/{urllib.parse.quote(variant.symbol)}",
            "Franklin": f"https://franklin.genoox.com/clinical-db/home?search={query}",
            "gnomAD": f"https://gnomad.broadinstitute.org/variant/{urllib.parse.quote(self._gnomad_variant_id(variant))}?dataset={self.settings.gnomad_dataset}",
        }
        return urls.get(database, "")

    def _review_query(self, variant: VariantRecord) -> str:
        return " ".join(part for part in [variant.symbol, variant.hgvsc, variant.hgvsp, variant.genomic_location] if part)

    def _cdna_without_transcript(self, hgvsc: str) -> str:
        return hgvsc.split(":", 1)[1] if ":" in hgvsc else hgvsc

    def _protein_change(self, hgvsp: str) -> str:
        if not hgvsp:
            return ""
        return hgvsp.split(":", 1)[-1].replace("p.", "").strip()

    def _at(self, values: list, index: int) -> str:
        return str(values[index]) if index < len(values) and values[index] is not None else ""

    def _unique_text(self, values: list) -> list[str]:
        unique = []
        for value in values:
            text = str(value)
            if text and text not in unique:
                unique.append(text)
        return unique
