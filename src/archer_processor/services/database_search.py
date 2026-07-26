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
    "Franklin": "Login workflow. Use gene, HGVSc/HGVSp, or genomic position.",
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
            elif database == "COSMIC":
                evidence.append(self._search_cosmic(variant))
            elif database == "OncoKB":
                evidence.append(self._search_oncokb(variant))
            elif database == "gnomAD":
                evidence.append(self._search_gnomad(variant))
            else:
                evidence.append(self._manual_evidence(database, variant))
        return evidence

    def _manual_evidence(self, database: str, variant: VariantRecord) -> DatabaseEvidence:
        query = variant.hgvsc or variant.genomic_location or variant.display_name
        return DatabaseEvidence(
            database=database,
            status="manual_required",
            summary=f"{MANUAL_DATABASES.get(database, 'Manual database check required')} Query: {query}",
            url=self._manual_url(database, variant),
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
        terms = " ".join(part for part in [variant.symbol, self._cdna_without_transcript(variant.hgvsc), variant.hgvsp] if part)
        if not terms:
            return DatabaseEvidence("COSMIC", "invalid_query", "Missing gene/HGVS fields.")
        fields = [
            "MutationID",
            "GeneName",
            "MutationCDS",
            "MutationAA",
            "MutationDescription",
            "MutationGenomePosition",
            "PrimarySite",
            "PrimaryHistology",
            "PubmedPMID",
        ]
        try:
            response = requests.get(
                "https://clinicaltables.nlm.nih.gov/api/cosmic/v3/search",
                params={
                    "terms": terms,
                    "maxList": 8,
                    "ef": ",".join(fields),
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            total = int(data[0]) if data else 0
            extras = data[2] or {}
            if total == 0:
                return DatabaseEvidence("COSMIC", "not_found", f"No COSMIC hit for {terms}.", url=self._manual_url("COSMIC", variant))
            ids = extras.get("MutationID", [])
            genes = extras.get("GeneName", [])
            cds = extras.get("MutationCDS", [])
            aa = extras.get("MutationAA", [])
            sites = extras.get("PrimarySite", [])
            first = []
            for index, mutation_id in enumerate(ids[:3]):
                bits = [
                    mutation_id,
                    self._at(genes, index),
                    self._at(cds, index),
                    self._at(aa, index),
                    self._at(sites, index),
                ]
                first.append(" ".join(bit for bit in bits if bit))
            return DatabaseEvidence(
                database="COSMIC",
                status="found",
                summary=f"{total} COSMIC match(es). " + "; ".join(first),
                accession=", ".join(ids[:5]),
                url=self._manual_url("COSMIC", variant),
                raw={"query": terms, "extra": extras},
            )
        except Exception as exc:
            return DatabaseEvidence("COSMIC", "error", f"COSMIC lookup failed: {exc}", url=self._manual_url("COSMIC", variant))

    def _search_oncokb(self, variant: VariantRecord) -> DatabaseEvidence:
        alteration = self._protein_change(variant.hgvsp) or self._cdna_without_transcript(variant.hgvsc)
        if not variant.symbol or not alteration:
            return DatabaseEvidence("OncoKB", "invalid_query", "Needs gene plus HGVSp or HGVSc.", url=self._manual_url("OncoKB", variant))
        if not self.settings.oncokb_api_key:
            return DatabaseEvidence(
                database="OncoKB",
                status="token_required",
                summary=f"OncoKB API requires a token. Query prepared: {variant.symbol} {alteration}.",
                url=self._manual_url("OncoKB", variant),
            )
        try:
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
            ]
            return DatabaseEvidence(
                database="OncoKB",
                status="found",
                summary="; ".join(part for part in summary_parts if not part.endswith("=")),
                clinical_significance=data.get("oncogenic", ""),
                url=self._manual_url("OncoKB", variant),
                raw=data,
            )
        except requests.HTTPError as exc:
            status = "unauthorized" if exc.response is not None and exc.response.status_code == 401 else "error"
            return DatabaseEvidence("OncoKB", status, f"OncoKB lookup failed: {exc}", url=self._manual_url("OncoKB", variant))
        except Exception as exc:
            return DatabaseEvidence("OncoKB", "error", f"OncoKB lookup failed: {exc}", url=self._manual_url("OncoKB", variant))

    def _search_gnomad(self, variant: VariantRecord) -> DatabaseEvidence:
        variant_id = self._gnomad_variant_id(variant)
        if not variant_id:
            return DatabaseEvidence(
                database="gnomAD",
                status="unsupported_query",
                summary="gnomAD live lookup needs genomic location plus ref/alt alleles.",
                url=self._manual_url("gnomAD", variant),
            )
        query = """
        query Variant($variantId: String!, $datasetId: DatasetId!) {
          variant(variantId: $variantId, dataset: $datasetId) {
            variant_id
            rsids
            exome { ac an filters }
            genome { ac an filters }
          }
        }
        """
        try:
            response = requests.post(
                "https://gnomad.broadinstitute.org/api/",
                json={"query": query, "variables": {"variantId": variant_id, "datasetId": "gnomad_r2_1"}},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            data = (payload.get("data") or {}).get("variant")
            if not data:
                return DatabaseEvidence("gnomAD", "not_found", f"No gnomAD v2.1 GRCh37 hit for {variant_id}.", url=self._manual_url("gnomAD", variant), raw=payload)
            exome = data.get("exome") or {}
            genome = data.get("genome") or {}
            af_parts = []
            for label, source in [("exome", exome), ("genome", genome)]:
                ac = source.get("ac")
                an = source.get("an")
                if ac is not None and an:
                    af_parts.append(f"{label}_AF={ac / an:.6g} ({ac}/{an})")
            return DatabaseEvidence(
                database="gnomAD",
                status="found",
                summary="; ".join(af_parts) or f"gnomAD record found for {variant_id}.",
                accession=", ".join(data.get("rsids") or []),
                url=self._manual_url("gnomAD", variant),
                raw=data,
            )
        except Exception as exc:
            return DatabaseEvidence("gnomAD", "error", f"gnomAD lookup failed: {exc}", url=self._manual_url("gnomAD", variant))

    def _gnomad_variant_id(self, variant: VariantRecord) -> str:
        if not variant.genomic_location or not variant.ref_allele or not variant.alt_allele:
            return ""
        try:
            chrom, pos = variant.genomic_location.replace("chr", "").split(":", 1)
            pos = pos.split("-", 1)[0]
            return f"{chrom}-{pos}-{variant.ref_allele}-{variant.alt_allele}"
        except ValueError:
            return ""

    def _manual_url(self, database: str, variant: VariantRecord) -> str:
        query = urllib.parse.quote(" ".join(part for part in [variant.symbol, variant.hgvsc, variant.hgvsp, variant.genomic_location] if part))
        urls = {
            "MTBP": "https://mtbp.herokuapp.com/",
            "HSMD": "https://variants.ingenuity.com/",
            "COSMIC": f"https://cancer.sanger.ac.uk/cosmic/search?q={query}",
            "OncoKB": f"https://www.oncokb.org/gene/{urllib.parse.quote(variant.symbol)}",
            "Franklin": f"https://franklin.genoox.com/clinical-db/home?search={query}",
            "gnomAD": f"https://gnomad.broadinstitute.org/variant/{urllib.parse.quote(self._gnomad_variant_id(variant))}?dataset=gnomad_r2_1",
        }
        return urls.get(database, "")

    def _cdna_without_transcript(self, hgvsc: str) -> str:
        return hgvsc.split(":", 1)[1] if ":" in hgvsc else hgvsc

    def _protein_change(self, hgvsp: str) -> str:
        if not hgvsp:
            return ""
        return hgvsp.split(":", 1)[-1].replace("p.", "").strip()

    def _at(self, values: list, index: int) -> str:
        return str(values[index]) if index < len(values) and values[index] is not None else ""
