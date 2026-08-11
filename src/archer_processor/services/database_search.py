from __future__ import annotations

import html
import csv
import re
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
from archer_processor.services.variant_identity import genomic_identity


MANUAL_DATABASES = {
    "MTBP": "Login-based research portal. Submit a pseudonymized variant list or VCF and record: classification, functional relevance/evidence category, population AF, ClinVar class, references, pipeline version, and notes. The public portal is research-only.",
    "HSMD": "Manual/licensed review. Record: classification, actionability tier, clinical review status, population frequency, references, notes.",
}

AMINO_ACID_3_TO_1 = {
    "Ala": "A",
    "Arg": "R",
    "Asn": "N",
    "Asp": "D",
    "Cys": "C",
    "Gln": "Q",
    "Glu": "E",
    "Gly": "G",
    "His": "H",
    "Ile": "I",
    "Leu": "L",
    "Lys": "K",
    "Met": "M",
    "Phe": "F",
    "Pro": "P",
    "Ser": "S",
    "Thr": "T",
    "Trp": "W",
    "Tyr": "Y",
    "Val": "V",
    "Ter": "*",
    "Sec": "U",
    "Pyl": "O",
}

COSMIC_FIELDS = [
    "AccessionNumber",
    "GeneCDS_Length",
    "GeneName",
    "HGNC_ID",
    "MutationAA",
    "MutationCDS",
    "MutationDescription",
    "MutationGenomePosition",
    "MutationStrand",
    "MutationID",
    "LegacyMutationID",
    "GenomicMutationID",
    "Name",
    "PrimaryHistology",
    "PrimarySite",
    "PubmedPMID",
    "Site",
    "GRChVer",
    "COSMIC_GENE_ID",
    "COSMIC_PHENOTYPE_ID",
]


class FranklinAuthenticationError(RuntimeError):
    pass


class DatabaseSearchService:
    _gnomad_lock = threading.Lock()
    _last_gnomad_request = 0.0
    _eutils_lock = threading.Lock()
    _last_eutils_request = 0.0

    def __init__(self, settings: AppSettings | None = None, timeout: int = 12) -> None:
        self.settings = settings or AppSettings.load()
        self.timeout = timeout
        self._clinvar_cache: dict[str, DatabaseEvidence] = {}
        self._gnomad_cache: dict[str, DatabaseEvidence] = {}
        self._oncokb_info_cache: dict | None = None
        self._cbio_gene_cache: dict[str, int | None] = {}
        self._cbio_mutation_cache: dict[int, list[dict]] = {}

    def database_diagnostics(self, databases: Iterable[str]) -> dict[str, str]:
        diagnostics = {}
        for database in databases:
            if database == "MTBP":
                diagnostics[database] = "web one-variant reports (login, research-only)"
            elif database in MANUAL_DATABASES:
                diagnostics[database] = "manual"
            elif database == "OncoKB" and not self.settings.oncokb_api_key:
                diagnostics[database] = "token required"
            elif database == "Franklin" and self.settings.franklin_api_key:
                diagnostics[database] = "ready"
            elif database == "Franklin":
                diagnostics[database] = "browser login/public review (Premium API not configured)"
            elif database == "gnomAD":
                diagnostics[database] = f"ready ({self.settings.gnomad_dataset}, rate limited)"
            elif database == "COSMIC":
                diagnostics[database] = (
                    "browser login (full page + screenshots); public basic lookup retained"
                )
            elif database == "CIViC":
                diagnostics[database] = "ready (open GraphQL)"
            elif database == "CancerMine":
                diagnostics[database] = "ready (cached cancer gene roles)"
            elif database == "DGIdb":
                diagnostics[database] = "context only (drug-gene, not MTB evidence)"
            elif database == "ClinGen Allele Registry":
                diagnostics[database] = "context only (allele ID/dbSNP cross-links)"
            elif database == "cBioPortal":
                diagnostics[database] = "ready (public cohort context)"
            elif database == "ClinVar":
                diagnostics[database] = "browser summary capture (NCBI E-utilities resolution)"
            elif database == "OncoKB":
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
            elif database == "CIViC":
                evidence.append(self._search_civic(variant))
            elif database == "CancerMine":
                evidence.append(self._search_cancermine(variant))
            elif database == "DGIdb":
                evidence.append(self._search_dgidb(variant))
            elif database == "ClinGen Allele Registry":
                evidence.append(self._search_clingen_allele_registry(variant))
            elif database == "cBioPortal":
                evidence.append(self._search_cbioportal(variant))
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
        expected = genomic_identity(variant)
        if expected is None:
            return DatabaseEvidence(
                "ClinVar", "invalid_query", "Missing exact GRCh37 genomic identity."
            )
        cache_key = "|".join(self._clinvar_queries(variant))
        if cache_key in self._clinvar_cache:
            return self._clinvar_cache[cache_key]
        try:
            candidate_ids: list[str] = []
            query_attempts: list[str] = []
            for candidate in self._clinvar_queries(variant):
                query_attempts.append(candidate)
                params = {
                    "db": "clinvar",
                    "term": candidate,
                    "retmode": "xml",
                    "retmax": "20",
                }
                if self.settings.clinvar_api_key:
                    params["api_key"] = self.settings.clinvar_api_key
                search = self._eutils_get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params=params,
                )
                search.raise_for_status()
                root = ET.fromstring(search.content)
                for node in root.findall(".//Id"):
                    if node.text and node.text not in candidate_ids:
                        candidate_ids.append(node.text)
            if not candidate_ids:
                evidence = DatabaseEvidence(
                    database="ClinVar",
                    status="not_found",
                    summary=f"No ClinVar result for {query}.",
                    url=self._clinvar_search_url(query),
                )
                self._clinvar_cache[cache_key] = evidence
                return evidence
            for clinvar_id in candidate_ids:
                fetch_params = {
                    "db": "clinvar",
                    "id": clinvar_id,
                    "retmode": "xml",
                    "rettype": "vcv",
                    "is_variationid": "true",
                }
                if self.settings.clinvar_api_key:
                    fetch_params["api_key"] = self.settings.clinvar_api_key
                fetch = self._eutils_get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params=fetch_params,
                )
                fetch.raise_for_status()
                matched = self._matching_clinvar_location(fetch.content, expected)
                if matched is None:
                    continue
                evidence = self._parse_clinvar(fetch.content, query, clinvar_id)
                evidence.raw.update(
                    {
                        "assembly_verified": "GRCh37",
                        "matched_location": matched,
                        "candidate_ids": candidate_ids,
                        "query_attempts": query_attempts,
                    }
                )
                self._clinvar_cache[cache_key] = evidence
                return evidence
            evidence = DatabaseEvidence(
                database="ClinVar",
                status="identity_mismatch",
                summary="ClinVar candidates were found, but none matched the exact GRCh37 locus and alleles.",
                url=self._clinvar_search_url(query),
                raw={
                    "candidate_ids": candidate_ids,
                    "query_attempts": query_attempts,
                    "expected_location": {
                        "assembly": expected.assembly,
                        "chromosome": expected.chromosome,
                        "position": expected.position,
                        "reference": expected.reference,
                        "alternate": expected.alternate,
                    },
                },
            )
            self._clinvar_cache[cache_key] = evidence
            return evidence
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                evidence = DatabaseEvidence(
                    database="ClinVar",
                    status="rate_limited",
                    summary="ClinVar/NCBI rate limit reached after retries. Try again later, lower Workers to 1, or add an NCBI API key in Settings.",
                    url=self._clinvar_search_url(query),
                )
                self._clinvar_cache[cache_key] = evidence
                return evidence
            return DatabaseEvidence(
                database="ClinVar",
                status="error",
                summary=f"ClinVar lookup failed: {exc}",
                url=self._clinvar_search_url(query),
            )
        except Exception as exc:
            return DatabaseEvidence(
                database="ClinVar",
                status="error",
                summary=f"ClinVar lookup failed: {exc}",
                url=self._clinvar_search_url(query),
            )

    def _eutils_get(self, url: str, params: dict) -> requests.Response:
        for attempt in range(4):
            self._wait_for_eutils_slot()
            response = requests.get(url, params=params, timeout=self.timeout)
            if response.status_code != 429:
                return response
            retry_after = response.headers.get("Retry-After")
            wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (attempt + 1)
            time.sleep(wait_seconds)
        return response

    def _wait_for_eutils_slot(self) -> None:
        min_interval = 0.12 if self.settings.clinvar_api_key else 0.42
        with self._eutils_lock:
            elapsed = time.monotonic() - self._last_eutils_request
            wait_seconds = max(0.0, min_interval - elapsed)
            if wait_seconds:
                time.sleep(wait_seconds)
            type(self)._last_eutils_request = time.monotonic()

    def _parse_clinvar(self, content: bytes, query: str, clinvar_id: str) -> DatabaseEvidence:
        text = content.decode("utf-8", errors="replace")
        root = ET.fromstring(content)
        archive = (
            root
            if root.tag.rsplit("}", 1)[-1] == "VariationArchive"
            else root.find(".//VariationArchive")
        )
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

    @staticmethod
    def _matching_clinvar_location(content: bytes, expected) -> dict | None:
        root = ET.fromstring(content)
        for node in root.findall(".//SequenceLocation"):
            assembly = node.attrib.get("Assembly", "")
            chromosome = node.attrib.get("Chr", "").removeprefix("chr").upper()
            if chromosome == "MT":
                chromosome = "M"
            position = node.attrib.get("positionVCF", "")
            reference = node.attrib.get(
                "referenceAlleleVCF", node.attrib.get("referenceAllele", "")
            ).upper()
            alternate = node.attrib.get(
                "alternateAlleleVCF", node.attrib.get("alternateAllele", "")
            ).upper()
            if not position.isdigit():
                continue
            if (
                assembly.startswith("GRCh37")
                and chromosome == expected.chromosome
                and int(position) == expected.position
                and reference == expected.reference
                and alternate == expected.alternate
            ):
                return {
                    "assembly": "GRCh37",
                    "chromosome": chromosome,
                    "position": int(position),
                    "reference": reference,
                    "alternate": alternate,
                }
        return None

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
        try:
            last_data = None
            for cosmic_query in queries:
                params = {
                    "terms": cosmic_query["terms"],
                    "maxList": 500,
                    "grchv": "37",
                    "ef": ",".join(COSMIC_FIELDS),
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
        histologies = extras.get("PrimaryHistology", [])
        pmids = extras.get("PubmedPMID", [])
        genomic_ids = extras.get("GenomicMutationID", [])
        phenotype_ids = extras.get("COSMIC_PHENOTYPE_ID", [])
        returned_count = max(
            [
                len(ids),
                *(len(value) for value in extras.values() if isinstance(value, list)),
            ],
            default=0,
        )
        records = [
            {
                field: self._at(extras.get(field, []), index)
                for field in COSMIC_FIELDS
            }
            for index in range(returned_count)
        ]
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
        unique_sites = self._split_unique_text(sites)
        unique_histologies = self._split_unique_text(histologies)
        unique_pmids = self._split_unique_text(pmids)
        summary_parts = [
            f"COSMIC public dataset: {total} match(es), {returned_count} record(s) returned for {query}",
            "IDs=" + ", ".join(self._unique_text(ids)[:8]),
        ]
        if unique_sites:
            summary_parts.append("primary_sites=" + ", ".join(unique_sites[:12]))
        if unique_histologies:
            summary_parts.append(
                "primary_histologies=" + ", ".join(unique_histologies[:12])
            )
        if unique_pmids:
            summary_parts.append(f"PubMed_references={len(unique_pmids)}")
        if first:
            summary_parts.append("examples=" + "; ".join(first))
        summary = "; ".join(part for part in summary_parts if part)
        return DatabaseEvidence(
            database="COSMIC",
            status="found",
            summary=summary.strip(),
            accession=", ".join(self._unique_text(ids)[:5]),
            url=self._manual_url("COSMIC", variant),
            raw={
                "query": query,
                "total_matches": total,
                "returned_count": returned_count,
                "fields": COSMIC_FIELDS,
                "records": records,
                "aggregates": {
                    "mutation_ids": self._unique_text(ids),
                    "legacy_mutation_ids": self._split_unique_text(legacy_ids),
                    "genomic_mutation_ids": self._split_unique_text(genomic_ids),
                    "primary_sites": unique_sites,
                    "primary_histologies": unique_histologies,
                    "pubmed_pmids": unique_pmids,
                    "phenotype_ids": self._split_unique_text(phenotype_ids),
                },
                "extra": extras,
            },
        )

    def _search_civic(self, variant: VariantRecord) -> DatabaseEvidence:
        if not variant.symbol:
            return DatabaseEvidence("CIViC", "invalid_query", "Needs gene plus variant name.", url=self._manual_url("CIViC", variant))
        variant_names = self._civic_variant_names(variant)
        if not variant_names:
            return DatabaseEvidence("CIViC", "invalid_query", "Needs HGVSp or HGVSc to build a CIViC variant query.", url=self._manual_url("CIViC", variant))
        try:
            last_payload = None
            for variant_name in variant_names:
                payload = self._civic_graphql(
                    """
                    query BrowseCivicProfiles($featureName: String!, $variantName: String!, $first: Int) {
                      browseMolecularProfiles(featureName: $featureName, variantName: $variantName, first: $first) {
                        filteredCount
                        nodes {
                          id
                          name
                          link
                          evidenceItemCount
                          assertionCount
                          diseases { name link }
                          therapies { name link }
                          variants { name link }
                        }
                      }
                    }
                    """,
                    {"featureName": variant.symbol, "variantName": variant_name, "first": 5},
                )
                last_payload = payload
                profiles = ((payload.get("data") or {}).get("browseMolecularProfiles") or {}).get("nodes") or []
                if profiles:
                    return self._format_civic_evidence(variant, variant_name, profiles[0], payload)
            return DatabaseEvidence(
                "CIViC",
                "not_found",
                f"No accepted CIViC molecular profile hit for {variant.symbol} {'; '.join(variant_names)}.",
                url=self._manual_url("CIViC", variant),
                raw={"variant_names": variant_names, "last_response": last_payload},
            )
        except Exception as exc:
            return DatabaseEvidence("CIViC", "error", f"CIViC lookup failed: {exc}", url=self._manual_url("CIViC", variant))

    def _format_civic_evidence(self, variant: VariantRecord, query: str, profile: dict, profile_payload: dict) -> DatabaseEvidence:
        evidence_payload = self._civic_graphql(
            """
            query CivicEvidence($molecularProfileId: Int, $first: Int) {
              evidenceItems(molecularProfileId: $molecularProfileId, status: ACCEPTED, first: $first) {
                totalCount
                nodes {
                  id
                  name
                  evidenceType
                  evidenceLevel
                  significance
                  evidenceDirection
                  status
                  variantOrigin
                  description
                  disease { name link }
                  therapies { name link }
                  source { citationId sourceType }
                }
              }
            }
            """,
            {"molecularProfileId": profile.get("id"), "first": 5},
        )
        evidence_items = ((evidence_payload.get("data") or {}).get("evidenceItems") or {}).get("nodes") or []
        total_evidence = ((evidence_payload.get("data") or {}).get("evidenceItems") or {}).get("totalCount")
        diseases = self._names(profile.get("diseases") or [])[:4]
        therapies = self._names(profile.get("therapies") or [])[:4]
        top = evidence_items[0] if evidence_items else {}
        source = top.get("source") or {}
        top_parts = [
            top.get("name"),
            top.get("evidenceType"),
            top.get("evidenceLevel"),
            top.get("significance"),
            top.get("evidenceDirection"),
            self._name(top.get("disease") or {}),
            f"{source.get('sourceType')}:{source.get('citationId')}" if source.get("citationId") else "",
        ]
        parts = [
            f"profile={profile.get('name')}",
            f"accepted_evidence={total_evidence if total_evidence is not None else profile.get('evidenceItemCount', 0)}",
            f"assertions={profile.get('assertionCount', 0)}",
        ]
        if diseases:
            parts.append("diseases=" + ", ".join(diseases))
        if therapies:
            parts.append("therapies=" + ", ".join(therapies))
        if top:
            parts.append("top_evidence=" + " ".join(str(part) for part in top_parts if part))
        return DatabaseEvidence(
            database="CIViC",
            status="found",
            summary="; ".join(parts),
            accession=f"CIViC MP{profile.get('id')} ({query})",
            clinical_significance=str(top.get("significance") or ""),
            url=self._absolute_civic_url(profile.get("link") or ""),
            raw={"profile_query": profile_payload, "evidence": evidence_payload},
        )

    def _civic_graphql(self, query: str, variables: dict) -> dict:
        response = requests.post(
            "https://civicdb.org/api/graphql",
            json={"query": query, "variables": variables},
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            message = payload["errors"][0].get("message") if isinstance(payload["errors"][0], dict) else str(payload["errors"][0])
            raise ValueError(message)
        return payload

    def _civic_variant_names(self, variant: VariantRecord) -> list[str]:
        candidates = []
        protein = self._protein_change(variant.hgvsp)
        protein_short = self._protein_three_letter_to_one_letter(protein)
        cdna = self._cdna_without_transcript(variant.hgvsc)
        for candidate in [protein_short, protein, cdna]:
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _protein_three_letter_to_one_letter(self, protein_change: str) -> str:
        if not protein_change:
            return ""
        clean = protein_change.strip().replace("(", "").replace(")", "")
        match = re.fullmatch(r"([A-Z][a-z]{2})(\d+)([A-Z][a-z]{2}|\*|Ter|=)", clean)
        if not match:
            return clean
        ref, pos, alt = match.groups()
        ref_short = AMINO_ACID_3_TO_1.get(ref)
        alt_short = "*" if alt in {"*", "Ter"} else AMINO_ACID_3_TO_1.get(alt)
        if not ref_short or not alt_short:
            return clean
        return f"{ref_short}{pos}{alt_short}"

    def _absolute_civic_url(self, link: str) -> str:
        return f"https://civicdb.org{link}" if link.startswith("/") else link

    def _search_cancermine(self, variant: VariantRecord) -> DatabaseEvidence:
        if not variant.symbol:
            return DatabaseEvidence("CancerMine", "invalid_query", "Needs gene symbol.", url=self._manual_url("CancerMine", variant))
        try:
            rows = self._cancermine_rows(variant.symbol)
            if not rows:
                return DatabaseEvidence("CancerMine", "not_found", f"No CancerMine cancer-role entries for {variant.symbol}.", url=self._manual_url("CancerMine", variant))
            return self._format_cancermine_evidence(variant, rows)
        except Exception as exc:
            return DatabaseEvidence("CancerMine", "error", f"CancerMine lookup failed: {exc}", url=self._manual_url("CancerMine", variant))

    def _format_cancermine_evidence(self, variant: VariantRecord, rows: list[dict]) -> DatabaseEvidence:
        sorted_rows = sorted(rows, key=lambda row: int(row.get("citation_count") or 0), reverse=True)
        role_counts: dict[str, int] = {}
        for row in sorted_rows:
            role = row.get("role") or "role"
            role_counts[role] = role_counts.get(role, 0) + int(row.get("citation_count") or 0)
        top_context = [
            f"{row.get('role')} in {row.get('cancer_normalized')} ({row.get('citation_count')} citations)"
            for row in sorted_rows[:6]
        ]
        parts = [
            f"gene={variant.symbol}",
            f"entries={len(sorted_rows)}",
            "role_citations=" + ", ".join(f"{role}:{count}" for role, count in sorted(role_counts.items())),
        ]
        if top_context:
            parts.append("top_context=" + "; ".join(top_context))
        return DatabaseEvidence(
            database="CancerMine",
            status="found",
            summary="; ".join(parts),
            accession=sorted_rows[0].get("gene_hugo_id") or variant.symbol,
            clinical_significance="text-mined cancer gene role",
            url=self._manual_url("CancerMine", variant),
            raw={"rows": sorted_rows[:25]},
        )

    def _cancermine_rows(self, gene_symbol: str) -> list[dict]:
        path = self._cancermine_cache_path()
        if not path.exists():
            response = requests.get(
                "https://zenodo.org/api/records/7689627/files/cancermine_collated.tsv/content",
                timeout=max(self.timeout, 30),
            )
            response.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(response.text, encoding="utf-8")
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [
                row for row in csv.DictReader(handle, delimiter="\t")
                if (row.get("gene_normalized") or "").upper() == gene_symbol.upper()
            ]

    def _cancermine_cache_path(self):
        return AppSettings.config_path().parent / "cancermine_collated.tsv"

    def _search_dgidb(self, variant: VariantRecord) -> DatabaseEvidence:
        if not variant.symbol:
            return DatabaseEvidence("DGIdb", "invalid_query", "Needs gene symbol.", url=self._manual_url("DGIdb", variant))
        try:
            payload = self._dgidb_graphql(
                """
                query DgidbGene($names: [String!]) {
                  genes(names: $names, first: 1) {
                    nodes {
                      name
                      conceptId
                      longName
                      interactions {
                        id
                        interactionScore
                        evidenceScore
                        interactionTypes { type directionality }
                        drug { name conceptId approved immunotherapy antiNeoplastic }
                        sources { sourceDbName }
                        publications { pmid }
                      }
                    }
                  }
                }
                """,
                {"names": [variant.symbol]},
            )
            genes = ((payload.get("data") or {}).get("genes") or {}).get("nodes") or []
            gene = genes[0] if genes else {}
            if not gene:
                return DatabaseEvidence("DGIdb", "not_found", f"No DGIdb gene match for {variant.symbol}.", url=self._manual_url("DGIdb", variant), raw=payload)
            return self._format_dgidb_evidence(variant, gene, payload)
        except Exception as exc:
            return DatabaseEvidence("DGIdb", "error", f"DGIdb lookup failed: {exc}", url=self._manual_url("DGIdb", variant))

    def _format_dgidb_evidence(self, variant: VariantRecord, gene: dict, payload: dict) -> DatabaseEvidence:
        interactions = gene.get("interactions") or []
        approved = []
        antineoplastic = []
        immunotherapy = []
        sources = []
        top = sorted(interactions, key=lambda item: item.get("evidenceScore") or 0, reverse=True)[:5]
        top_drugs = []
        for item in interactions:
            drug = item.get("drug") or {}
            name = drug.get("name")
            if not name:
                continue
            if drug.get("approved"):
                approved.append(name)
            if drug.get("antiNeoplastic"):
                antineoplastic.append(name)
            if drug.get("immunotherapy"):
                immunotherapy.append(name)
            for source in item.get("sources") or []:
                if source.get("sourceDbName"):
                    sources.append(source["sourceDbName"])
        for item in top:
            drug = item.get("drug") or {}
            interaction_types = [entry.get("type") for entry in item.get("interactionTypes") or [] if entry.get("type")]
            label = str(drug.get("name") or "")
            if interaction_types:
                label += f" ({', '.join(interaction_types[:2])})"
            if item.get("evidenceScore") is not None:
                label += f" evidence={item.get('evidenceScore')}"
            if label:
                top_drugs.append(label)
        parts = [
            f"gene={gene.get('name')}",
            f"interactions={len(interactions)}",
            f"approved={len(set(approved))}",
            f"antineoplastic={len(set(antineoplastic))}",
        ]
        if immunotherapy:
            parts.append(f"immunotherapy={len(set(immunotherapy))}")
        if top_drugs:
            parts.append("top_drugs=" + "; ".join(top_drugs))
        if sources:
            parts.append("sources=" + ", ".join(self._unique_text(sources)[:8]))
        return DatabaseEvidence(
            database="DGIdb",
            status="found" if interactions else "not_found",
            summary="; ".join(parts),
            accession=str(gene.get("conceptId") or variant.symbol),
            clinical_significance="drug-gene context" if interactions else "",
            url=self._manual_url("DGIdb", variant),
            raw=payload,
        )

    def _dgidb_graphql(self, query: str, variables: dict) -> dict:
        response = requests.post(
            "https://dgidb.org/api/graphql",
            json={"query": query, "variables": variables},
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            message = payload["errors"][0].get("message") if isinstance(payload["errors"][0], dict) else str(payload["errors"][0])
            raise ValueError(message)
        return payload

    def _search_clingen_allele_registry(self, variant: VariantRecord) -> DatabaseEvidence:
        query = variant.hgvsc or variant.dbsnp_id or variant.cosmic_id
        if not query:
            return DatabaseEvidence("ClinGen Allele Registry", "invalid_query", "Needs HGVSc or known identifier.", url=self._manual_url("ClinGen Allele Registry", variant))
        try:
            response = requests.get(
                "https://reg.clinicalgenome.org/allele",
                params={"hgvs": query},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
            if response.status_code == 404:
                return DatabaseEvidence("ClinGen Allele Registry", "not_found", f"No ClinGen Allele Registry hit for {query}.", accession=query, url=self._manual_url("ClinGen Allele Registry", variant))
            if response.status_code == 400:
                data = response.json()
                return DatabaseEvidence(
                    "ClinGen Allele Registry",
                    "invalid_query",
                    f"ClinGen Allele Registry rejected {query}: {data.get('message') or data.get('description') or 'invalid HGVS'}.",
                    accession=query,
                    url=self._manual_url("ClinGen Allele Registry", variant),
                    raw=data,
                )
            response.raise_for_status()
            data = response.json()
            return self._format_clingen_evidence(variant, query, data)
        except Exception as exc:
            return DatabaseEvidence("ClinGen Allele Registry", "error", f"ClinGen Allele Registry lookup failed: {exc}", url=self._manual_url("ClinGen Allele Registry", variant))

    def _format_clingen_evidence(self, variant: VariantRecord, query: str, data: dict) -> DatabaseEvidence:
        caid = str(data.get("@id") or "").rsplit("/", 1)[-1]
        titles = data.get("communityStandardTitle") or []
        external = data.get("externalRecords") or {}
        external_counts = []
        external_ids = []
        for source, records in external.items():
            if isinstance(records, list) and records:
                external_counts.append(f"{source}={len(records)}")
                external_ids.extend(str(record.get("id")) for record in records[:3] if isinstance(record, dict) and record.get("id"))
        summary_parts = [
            f"CAid={caid}" if caid else "",
            f"title={titles[0]}" if titles else "",
        ]
        if external_counts:
            summary_parts.append("external_records=" + ", ".join(external_counts[:8]))
        if external_ids:
            summary_parts.append("ids=" + ", ".join(self._unique_text(external_ids)[:10]))
        return DatabaseEvidence(
            database="ClinGen Allele Registry",
            status="found" if caid else "not_found",
            summary="; ".join(part for part in summary_parts if part),
            accession=caid or query,
            clinical_significance="allele normalization",
            url=f"https://reg.clinicalgenome.org/redmine/projects/registry/genboree_registry/by_caid?caid={urllib.parse.quote(caid)}" if caid else self._manual_url("ClinGen Allele Registry", variant),
            raw=data,
        )

    def _search_cbioportal(self, variant: VariantRecord) -> DatabaseEvidence:
        if not variant.symbol:
            return DatabaseEvidence("cBioPortal", "invalid_query", "Needs gene symbol.", url=self._manual_url("cBioPortal", variant))
        try:
            entrez_id = self._cbio_entrez_id(variant.symbol)
            if not entrez_id:
                return DatabaseEvidence("cBioPortal", "not_found", f"No cBioPortal gene match for {variant.symbol}.", url=self._manual_url("cBioPortal", variant))
            mutations = self._cbio_mutations(entrez_id)
            return self._format_cbioportal_evidence(variant, entrez_id, mutations)
        except Exception as exc:
            return DatabaseEvidence("cBioPortal", "error", f"cBioPortal lookup failed: {exc}", url=self._manual_url("cBioPortal", variant))

    def _format_cbioportal_evidence(self, variant: VariantRecord, entrez_id: int, mutations: list[dict]) -> DatabaseEvidence:
        protein = self._protein_change(variant.hgvsp)
        exact = [
            item for item in mutations
            if protein and str(item.get("proteinChange") or "").lower() == protein.lower()
        ]
        position = self._protein_position(protein)
        same_position = [
            item for item in mutations
            if position is not None and item.get("proteinPosStart") == position
        ]
        cancer_types = self._unique_text([str(item.get("cancerType") or item.get("studyId") or "") for item in exact])[:6]
        mutation_types = self._unique_text([str(item.get("mutationType") or "") for item in exact])[:6]
        parts = [
            "study=MSK-IMPACT 2017 public cohort",
            f"gene_mutations={len(mutations)}",
            f"exact_protein_matches={len(exact)}" if protein else "exact_protein_matches=not_available",
        ]
        if position is not None:
            parts.append(f"same_protein_position={len(same_position)}")
        if mutation_types:
            parts.append("exact_types=" + ", ".join(mutation_types))
        if cancer_types:
            parts.append("exact_context=" + ", ".join(cancer_types))
        return DatabaseEvidence(
            database="cBioPortal",
            status="found" if mutations else "not_found",
            summary="; ".join(parts),
            accession=f"{variant.symbol} Entrez:{entrez_id}",
            clinical_significance="public cohort frequency context" if mutations else "",
            url=self._manual_url("cBioPortal", variant),
            raw={"entrez_id": entrez_id, "exact_matches_preview": exact[:10], "mutation_count": len(mutations)},
        )

    def _cbio_entrez_id(self, gene_symbol: str) -> int | None:
        if gene_symbol in self._cbio_gene_cache:
            return self._cbio_gene_cache[gene_symbol]
        response = requests.get(
            f"https://www.cbioportal.org/api/genes/{urllib.parse.quote(gene_symbol)}",
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            self._cbio_gene_cache[gene_symbol] = None
            return None
        response.raise_for_status()
        entrez_id = response.json().get("entrezGeneId")
        self._cbio_gene_cache[gene_symbol] = int(entrez_id) if entrez_id is not None else None
        return self._cbio_gene_cache[gene_symbol]

    def _cbio_mutations(self, entrez_id: int) -> list[dict]:
        if entrez_id in self._cbio_mutation_cache:
            return self._cbio_mutation_cache[entrez_id]
        response = requests.post(
            "https://www.cbioportal.org/api/molecular-profiles/msk_impact_2017_mutations/mutations/fetch",
            params={"projection": "DETAILED"},
            json={"entrezGeneIds": [entrez_id], "sampleListId": "msk_impact_2017_all"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=max(self.timeout, 30),
        )
        response.raise_for_status()
        mutations = response.json()
        self._cbio_mutation_cache[entrez_id] = mutations if isinstance(mutations, list) else []
        return self._cbio_mutation_cache[entrez_id]

    def _protein_position(self, protein_change: str) -> int | None:
        digits = "".join(char for char in protein_change if char.isdigit())
        return int(digits) if digits else None

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
        try:
            token = self._franklin_token()
            if not token:
                return DatabaseEvidence(
                    database="Franklin",
                    status="web_review_required",
                    summary=(
                        "Franklin's public variant page is available for review. "
                        "Supported API automation requires Franklin Premium access; "
                        f"query prepared: {query}."
                    ),
                    accession=query,
                    url=self._franklin_public_url(query),
                )
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
        except FranklinAuthenticationError as exc:
            return DatabaseEvidence("Franklin", "unauthorized", str(exc), accession=query, url=self._manual_url("Franklin", variant))
        except requests.HTTPError as exc:
            status = "unauthorized" if exc.response is not None and exc.response.status_code in {401, 403} else "error"
            return DatabaseEvidence("Franklin", status, f"Franklin lookup failed: {exc}", url=self._manual_url("Franklin", variant))
        except Exception as exc:
            return DatabaseEvidence("Franklin", "error", f"Franklin lookup failed: {exc}", url=self._manual_url("Franklin", variant))

    def _franklin_token(self) -> str:
        return self.settings.franklin_api_key

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
            value = info.get(key)
            if isinstance(value, dict) and value.get("version"):
                return str(value["version"])
            if value:
                return str(value)
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
        cdna = self._cdna_without_transcript(variant.hgvsc)
        if variant.symbol and cdna:
            return f"{variant.symbol}:{cdna}"
        if variant.genomic_location and variant.ref_allele and variant.alt_allele:
            try:
                chrom, pos = variant.genomic_location.split(":", 1)
                pos = pos.split("-", 1)[0]
                if not chrom.lower().startswith("chr"):
                    chrom = f"chr{chrom}"
                return f"{chrom}-{pos}-{variant.ref_allele}-{variant.alt_allele}"
            except ValueError:
                pass
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
        return self._franklin_public_url(self._franklin_search_text(variant))

    def _franklin_public_url(self, query: str) -> str:
        """Return Franklin search; direct genomic routes can mis-handle strand orientation."""
        return "https://franklin.genoox.com/clinical-db/home"

    def _manual_url(self, database: str, variant: VariantRecord) -> str:
        query = urllib.parse.quote(self._review_query(variant))
        urls = {
            "MTBP": "https://mtbp.org/analyse/",
            "HSMD": "https://variants.ingenuity.com/",
            "COSMIC": f"https://cancer.sanger.ac.uk/cosmic/search?q={query}",
            "CIViC": f"https://civicdb.org/search?query={query}",
            "CancerMine": f"https://github.com/jakelever/cancermine/search?q={urllib.parse.quote(variant.symbol)}",
            "DGIdb": f"https://dgidb.org/results?searchType=gene&searchTerms={urllib.parse.quote(variant.symbol)}",
            "ClinGen Allele Registry": f"https://reg.clinicalgenome.org/redmine/projects/registry/genboree_registry/landing?search={query}",
            "cBioPortal": f"https://www.cbioportal.org/results/mutations?Action=Submit&cancer_study_list=msk_impact_2017&case_set_id=msk_impact_2017_all&gene_list={urllib.parse.quote(variant.symbol)}",
            "OncoKB": f"https://www.oncokb.org/gene/{urllib.parse.quote(variant.symbol)}",
            "Franklin": self._franklin_public_url(self._franklin_search_text(variant)),
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
        return hgvsp.split(":", 1)[-1].replace("p.", "").replace("p=", "=").strip()

    def _at(self, values: list, index: int) -> str:
        return str(values[index]) if index < len(values) and values[index] is not None else ""

    def _unique_text(self, values: list) -> list[str]:
        unique = []
        for value in values:
            text = str(value)
            if text and text not in unique:
                unique.append(text)
        return unique

    def _split_unique_text(self, values: list) -> list[str]:
        unique: list[str] = []
        for value in values:
            for part in re.split(r"\s*;\s*", str(value or "")):
                if part and part not in unique:
                    unique.append(part)
        return unique

    def _name(self, value: dict) -> str:
        return str(value.get("name") or "")

    def _names(self, values: list[dict]) -> list[str]:
        return [name for item in values if (name := self._name(item))]
