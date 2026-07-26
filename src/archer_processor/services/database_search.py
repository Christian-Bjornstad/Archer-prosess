from __future__ import annotations

import html
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Iterable

import requests

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.settings import AppSettings


MANUAL_DATABASES = {
    "MTBP": "Licensed/manual workflow. Search in MTBP Karolinska and record evidence.",
    "HSMD": "Licensed/manual workflow. Search by variant, gene alteration, or genomic position.",
    "COSMIC": "Token/licensed workflow. Use GRCh37/hg19 for VPM interpretation.",
    "OncoKB": "Token workflow. Use gene and protein/HGVSc alteration.",
    "Franklin": "Login workflow. Use gene, HGVSc/HGVSp, or genomic position.",
    "gnomAD": "Manual web check recommended unless a stable API endpoint is configured.",
}


class DatabaseSearchService:
    def __init__(self, settings: AppSettings | None = None, timeout: int = 12) -> None:
        self.settings = settings or AppSettings.load()
        self.timeout = timeout

    def search_variant(self, variant: VariantRecord, databases: Iterable[str]) -> list[DatabaseEvidence]:
        evidence: list[DatabaseEvidence] = []
        for database in databases:
            if database == "ClinVar":
                evidence.append(self._search_clinvar(variant))
            else:
                evidence.append(self._manual_evidence(database, variant))
        return evidence

    def _manual_evidence(self, database: str, variant: VariantRecord) -> DatabaseEvidence:
        query = variant.hgvsc or variant.genomic_location or variant.display_name
        return DatabaseEvidence(
            database=database,
            status="manual_required",
            summary=f"{MANUAL_DATABASES.get(database, 'Manual database check required')} Query: {query}",
        )

    def _search_clinvar(self, variant: VariantRecord) -> DatabaseEvidence:
        query = variant.hgvsc
        if not query:
            return DatabaseEvidence("ClinVar", "invalid_query", "Missing HGVSc.")
        params = {
            "db": "clinvar",
            "term": f"{query}[VARNAME]",
            "retmode": "xml",
            "retmax": "1",
        }
        if self.settings.clinvar_api_key:
            params["api_key"] = self.settings.clinvar_api_key
        try:
            search = requests.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params=params,
                timeout=self.timeout,
            )
            search.raise_for_status()
            root = ET.fromstring(search.content)
            clinvar_id = root.findtext(".//Id")
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
            }
            if self.settings.clinvar_api_key:
                fetch_params["api_key"] = self.settings.clinvar_api_key
            fetch = requests.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params=fetch_params,
                timeout=self.timeout,
            )
            fetch.raise_for_status()
            return self._parse_clinvar(fetch.content, query, clinvar_id)
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
        title = root.findtext(".//Title") or root.findtext(".//Name/ElementValue") or query
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
        accession = ""
        accession_node = root.find(".//*[@Accession]")
        if accession_node is not None:
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
