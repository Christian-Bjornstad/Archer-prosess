from pathlib import Path

import requests

from archer_processor.core.models import DatabaseEvidence
from archer_processor.io import ArcherTsvReader
from archer_processor.services import AppSettings
from archer_processor.services.database_search import DatabaseSearchService


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


class FakeDatabaseSearchService(DatabaseSearchService):
    def search_variant(self, variant, databases):
        return [
            DatabaseEvidence(
                database=database,
                status="found",
                summary=f"{variant.symbol} from {database}",
            )
            for database in databases
        ]


def test_parallel_search_returns_all_variants_and_progress():
    variants = ArcherTsvReader().read(FIXTURE)
    seen = []

    results = FakeDatabaseSearchService().search_variants_parallel(
        variants,
        ["ClinVar", "COSMIC"],
        max_workers=3,
        progress=lambda done, total, variant: seen.append((done, total, variant.hgvsc)),
    )

    assert len(results) == len(variants)
    assert len(seen) == len(variants)
    assert seen[-1][0] == len(variants)
    assert all(len(items) == 2 for items in results.values())


def test_database_diagnostics_reports_ready_token_and_manual_sources():
    settings = AppSettings(enabled_databases=["ClinVar"], oncokb_api_key="", franklin_api_key="")
    service = DatabaseSearchService(settings)

    diagnostics = service.database_diagnostics([
        "ClinVar",
        "gnomAD",
        "COSMIC",
        "CIViC",
        "CancerMine",
        "DGIdb",
        "ClinGen Allele Registry",
        "cBioPortal",
        "OncoKB",
        "Franklin",
        "MTBP",
        "HSMD",
    ])

    assert diagnostics["ClinVar"].startswith("browser summary capture")
    assert diagnostics["gnomAD"].startswith("ready")
    assert diagnostics["COSMIC"].startswith("browser login")
    assert diagnostics["CIViC"] == "ready (open GraphQL)"
    assert diagnostics["CancerMine"] == "ready (cached cancer gene roles)"
    assert diagnostics["DGIdb"] == "context only (drug-gene, not MTB evidence)"
    assert diagnostics["ClinGen Allele Registry"] == "context only (allele ID/dbSNP cross-links)"
    assert diagnostics["cBioPortal"] == "ready (public cohort context)"
    assert diagnostics["OncoKB"] == "token required"
    assert diagnostics["Franklin"] == "browser login/public review (Premium API not configured)"
    assert diagnostics["MTBP"] == "web one-variant reports (login, research-only)"
    assert diagnostics["HSMD"] == "manual"


def test_database_diagnostics_keeps_browser_password_separate_from_api_access():
    settings = AppSettings(franklin_api_key="", franklin_email="user@example.org", franklin_password="secret")
    service = DatabaseSearchService(settings)

    diagnostics = service.database_diagnostics(["Franklin"])

    assert diagnostics["Franklin"] == "browser login/public review (Premium API not configured)"


def test_manual_sources_return_checklist_and_query():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService()

    mtbp = service._manual_evidence("MTBP", variant)
    hsmd = service._manual_evidence("HSMD", variant)

    assert mtbp.status == "manual"
    assert "functional relevance" in mtbp.summary
    assert "TP53" in mtbp.accession
    assert hsmd.status == "manual"
    assert "actionability tier" in hsmd.summary


def test_clinvar_rate_limit_is_reported_after_retries(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(b"", status_code=429)

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)
    monkeypatch.setattr("archer_processor.services.database_search.time.sleep", lambda seconds: None)

    evidence = service._search_clinvar(variant)

    assert evidence.status == "rate_limited"
    assert "lower Workers to 1" in evidence.summary
    assert len(calls) == 4


def test_clinvar_accepts_only_exact_grch37_candidate(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def xml_response(text):
        response = FakeResponse({})
        response.content = text.encode()
        return response

    def fake_get(url, params):
        if "esearch" in url:
            return xml_response("<eSearchResult><IdList><Id>38</Id><Id>37</Id></IdList></eSearchResult>")
        if params["id"] == "38":
            return xml_response(
                '<VariationArchive Accession="VCV38" Version="1" VariationName="wrong">'
                '<SequenceLocation Assembly="GRCh38" Chr="17" positionVCF="7675088" '
                'referenceAlleleVCF="G" alternateAlleleVCF="A"/></VariationArchive>'
            )
        return xml_response(
            '<VariationArchive Accession="VCV37" Version="2" VariationName="TP53">'
            '<SequenceLocation Assembly="GRCh37" Chr="17" positionVCF="7578406" '
            'referenceAlleleVCF="G" alternateAlleleVCF="A"/>'
            '<GermlineClassification><Description>Pathogenic</Description>'
            '</GermlineClassification></VariationArchive>'
        )

    monkeypatch.setattr(service, "_eutils_get", fake_get)
    evidence = service._search_clinvar(variant)

    assert evidence.status == "found"
    assert evidence.accession == "VCV37.2"
    assert evidence.raw["assembly_verified"] == "GRCh37"
    assert evidence.raw["matched_location"]["position"] == 7578406


def test_clinvar_automatic_queries_are_exact_and_grch37_scoped():
    variant = ArcherTsvReader().read(FIXTURE)[3]

    assert DatabaseSearchService()._clinvar_queries(variant) == [
        "NM_000546.6:c.524G>A[VARNAME]",
        "rs28934578",
        "17[chr] AND 7578406[chrpos37]",
    ]


def test_clinvar_fails_closed_when_candidates_do_not_match_grch37(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def xml_response(text):
        response = FakeResponse({})
        response.content = text.encode()
        return response

    search_calls = 0

    def fake_get(url, params):
        nonlocal search_calls
        if "esearch" in url:
            search_calls += 1
            ids = "<Id>99</Id>" if search_calls == 1 else ""
            return xml_response(f"<eSearchResult><IdList>{ids}</IdList></eSearchResult>")
        return xml_response(
            '<VariationArchive Accession="VCV99" Version="1">'
            '<SequenceLocation Assembly="GRCh38" Chr="17" positionVCF="7675088" '
            'referenceAlleleVCF="G" alternateAlleleVCF="A"/></VariationArchive>'
        )

    monkeypatch.setattr(service, "_eutils_get", fake_get)

    evidence = service._search_clinvar(variant)

    assert evidence.status == "identity_mismatch"
    assert evidence.raw["candidate_ids"] == ["99"]


def test_clinvar_treats_coordinate_only_candidates_as_not_found(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    variant.dbsnp_id = ""
    service = DatabaseSearchService(timeout=1)

    def xml_response(text):
        response = FakeResponse({})
        response.content = text.encode()
        return response

    search_calls = 0

    def fake_get(url, params):
        nonlocal search_calls
        if "esearch" in url:
            search_calls += 1
            ids = "" if search_calls == 1 else "<Id>99</Id>"
            return xml_response(f"<eSearchResult><IdList>{ids}</IdList></eSearchResult>")
        return xml_response(
            '<VariationArchive Accession="VCV99" Version="1">'
            '<SequenceLocation Assembly="GRCh37" Chr="17" positionVCF="7578406" '
            'referenceAlleleVCF="G" alternateAlleleVCF="T"/></VariationArchive>'
        )

    monkeypatch.setattr(service, "_eutils_get", fake_get)

    evidence = service._search_clinvar(variant)

    assert evidence.status == "not_found"
    assert "exact GRCh37 allele" in evidence.summary


def test_clinvar_queries_include_archer_dbsnp_identifier():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    variant.dbsnp_id = "rs28934578; rs123"

    assert DatabaseSearchService()._clinvar_queries(variant) == [
        "NM_000546.6:c.524G>A[VARNAME]",
        "rs28934578",
        "rs123",
        "17[chr] AND 7578406[chrpos37]",
    ]


def test_gnomad_formatter_reports_population_frequency_context():
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService()
    evidence = service._format_gnomad_evidence(
        variant,
        "12-111856588-C-A",
        {
            "variant_id": "12-111856588-C-A",
            "rsids": ["rs111360561"],
            "exome": {
                "ac": 10,
                "an": 20000,
                "af": 0.0005,
                "homozygote_count": 0,
                "hemizygote_count": 0,
                "filters": [],
                "populations": [{"id": "nfe", "ac": 4, "an": 4000, "homozygote_count": 0}],
                "faf95": {"popmax": 0.0012, "popmax_population": "nfe"},
            },
            "genome": {
                "ac": 2,
                "an": 10000,
                "af": 0.0002,
                "homozygote_count": 0,
                "hemizygote_count": 0,
                "filters": ["PASS"],
                "populations": [{"id": "amr", "ac": 3, "an": 2000, "homozygote_count": 0}],
                "faf95": {"popmax": 0.0008, "popmax_population": "amr"},
            },
        },
    )

    assert evidence.status == "found"
    assert evidence.accession == "rs111360561"
    assert "aggregated_AF=0.0400%" in evidence.summary
    assert "max_population_AF=0.1500%" in evidence.summary
    assert "frequency_context=low_frequency_population_variant" in evidence.summary


def test_gnomad_query_builder_uses_grch37_archer_coordinates():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService()

    assert service._gnomad_variant_id(variant) == "17-7578406-G-A"


def test_gnomad_not_found_handles_graphql_errors(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)
    service._wait_for_gnomad_slot = lambda: None

    def fake_post(*args, **kwargs):
        return FakeResponse({"errors": [{"message": "Variant not found"}], "data": {"variant": None}})

    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    evidence = service._search_gnomad(variant)

    assert evidence.status == "not_found"
    assert evidence.accession == "17-7578406-G-A"
    assert "Variant not found" in evidence.summary


def test_gnomad_rate_limited(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)
    service._wait_for_gnomad_slot = lambda: None

    def fake_post(*args, **kwargs):
        return FakeResponse({}, status_code=429)

    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    evidence = service._search_gnomad(variant)

    assert evidence.status == "rate_limited"


def test_franklin_without_token_prepares_public_review():
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService()

    evidence = service._search_franklin(variant)

    assert evidence.status == "web_review_required"
    assert "query prepared: FLT3:c.1419-4dup" in evidence.summary
    assert evidence.url == "https://franklin.genoox.com/clinical-db/home"


def test_franklin_browser_password_does_not_attempt_api_login(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService(
        AppSettings(franklin_api_key="", franklin_email="user@example.org", franklin_password="secret"),
        timeout=1,
    )
    monkeypatch.setattr(
        "archer_processor.services.database_search.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("API must not be called")),
    )

    evidence = service._search_franklin(variant)

    assert evidence.status == "web_review_required"


def test_franklin_token_parser_accepts_common_shapes():
    service = DatabaseSearchService()

    assert service._token_from_payload("plain-token") == "plain-token"
    assert service._token_from_payload({"apiToken": "api-token"}) == "api-token"
    assert service._token_from_payload({"data": {"access_token": "access-token"}}) == "access-token"


def test_franklin_unauthorized_is_reported(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService(AppSettings(franklin_api_key="bad-token"), timeout=1)

    def fake_get(*args, **kwargs):
        return FakeResponse({"detail": "bad token"}, status_code=401)

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_franklin(variant)

    assert evidence.status == "unauthorized"


def test_franklin_malformed_response_is_error(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService(AppSettings(franklin_api_key="token"), timeout=1)

    def fake_get(*args, **kwargs):
        return FakeResponse(ValueError("bad json"), json_error=True)

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_franklin(variant)

    assert evidence.status == "error"


def test_franklin_formatter_extracts_clinical_evidence():
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService()

    evidence = service._format_franklin_evidence(
        variant,
        "chr12-111856588-C-A",
        {
            "variants": [
                {
                    "link": "https://franklin.genoox.com/clinical-db/variant/example",
                    "classification": {
                        "acmg_classification": "likely benign",
                        "acmg_rules": [{"name": "BS1", "is_met": True}, {"name": "PM2", "is_met": False}],
                    },
                    "annotations": {
                        "frequencies": [
                            {"source": "gnomAD", "population": "nfe", "frequency": 0.00146},
                        ],
                        "predictions": {
                            "revel": {"score": 0.13},
                            "aggregated_predictions": "benign",
                        },
                        "clinical_evidences": {
                            "clinvar": {
                                "submissions_by_classification": [
                                    {"classification": "Uncertain significance", "count": 4}
                                ]
                            }
                        },
                        "transcripts": [
                            {"transcript_type": "REFSEQ", "transcript": "NM_005475.3", "cdot": "c.639C>A", "pdot": "p.Ser213Arg"}
                        ],
                    },
                }
            ]
        },
    )

    assert evidence.status == "found"
    assert evidence.clinical_significance == "likely benign"
    assert "rules=BS1" in evidence.summary
    assert "gnomAD:nfe=0.00146" in evidence.summary
    assert "revel=0.13" in evidence.summary
    assert "ClinVar=Uncertain significance:4" in evidence.summary


def test_cosmic_basic_found_uses_v4_and_public_label(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        if len(calls) == 1:
            return FakeResponse([0, [], {}, []])
        return FakeResponse(
            [
                1,
                ["10648"],
                {
                    "MutationID": ["10648"],
                    "LegacyMutationID": ["COSM10648"],
                    "GeneName": ["TP53"],
                    "MutationCDS": ["c.524G>A"],
                    "MutationAA": ["p.R175H"],
                    "MutationDescription": ["Substitution"],
                    "MutationGenomePosition": ["17:7578406-7578406"],
                    "GRChVer": ["37"],
                    "PrimarySite": ["haematopoietic_and_lymphoid_tissue"],
                },
                [],
            ]
        )

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_cosmic(variant)

    assert calls[0][0].endswith("/api/cosmic/v4/search")
    assert calls[0][1]["terms"] == "TP53"
    assert calls[0][1]["q"] == 'LegacyMutationID:"COSM10648" OR MutationID:"10648"'
    assert calls[1][1]["terms"] == "TP53 c.524G>A"
    assert evidence.status == "found"
    assert "COSMIC public dataset" in evidence.summary
    assert evidence.accession == "10648"
    assert evidence.raw["returned_count"] == 1
    assert evidence.raw["records"][0]["MutationCDS"] == "c.524G>A"
    assert evidence.raw["aggregates"]["primary_sites"] == [
        "haematopoietic_and_lymphoid_tissue"
    ]


def test_cosmic_basic_not_found(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def fake_get(*args, **kwargs):
        return FakeResponse([0, [], {}, []])

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_cosmic(variant)

    assert evidence.status == "not_found"
    assert "basic/public" in evidence.summary


def test_oncokb_without_token_prepares_query():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(AppSettings(oncokb_api_key=""))

    evidence = service._search_oncokb(variant)

    assert evidence.status == "token_required"
    assert evidence.accession == "TP53 R175H"


def test_civic_found_uses_v2_graphql(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json))
        if "BrowseCivicProfiles" in json["query"]:
            assert json["variables"]["featureName"] == "TP53"
            assert json["variables"]["variantName"] == "R175H"
            return FakeResponse(
                {
                    "data": {
                        "browseMolecularProfiles": {
                            "filteredCount": 1,
                            "nodes": [
                                {
                                    "id": 116,
                                    "name": "TP53 R175H",
                                    "link": "/molecular-profiles/116",
                                    "evidenceItemCount": 12,
                                    "assertionCount": 0,
                                    "diseases": [{"name": "Breast Cancer"}],
                                    "therapies": [{"name": "Doxorubicin"}],
                                    "variants": [{"name": "R175H"}],
                                }
                            ],
                        }
                    }
                }
            )
        return FakeResponse(
            {
                "data": {
                    "evidenceItems": {
                        "totalCount": 6,
                        "nodes": [
                            {
                                "name": "EID389",
                                "evidenceType": "PROGNOSTIC",
                                "evidenceLevel": "B",
                                "significance": "POOR_OUTCOME",
                                "evidenceDirection": "SUPPORTS",
                                "disease": {"name": "Breast Cancer"},
                                "therapies": [],
                                "source": {"citationId": "16489069", "sourceType": "PUBMED"},
                            }
                        ],
                    }
                }
            }
        )

    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    evidence = service._search_civic(variant)

    assert evidence.status == "found"
    assert evidence.accession == "CIViC MP116 (R175H)"
    assert evidence.clinical_significance == "POOR_OUTCOME"
    assert evidence.url == "https://civicdb.org/molecular-profiles/116"
    assert "accepted_evidence=6" in evidence.summary
    assert "therapies=Doxorubicin" in evidence.summary
    assert len(calls) == 2


def test_civic_converts_three_letter_hgvsp_to_short_variant_name(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    variant.hgvsp = "NP_000537.3:p.Arg175His"
    service = DatabaseSearchService(timeout=1)
    variant_names = []

    def fake_post(url, json, headers, timeout):
        if "BrowseCivicProfiles" in json["query"]:
            variant_names.append(json["variables"]["variantName"])
            return FakeResponse({"data": {"browseMolecularProfiles": {"filteredCount": 0, "nodes": []}}})
        return FakeResponse({"data": {"evidenceItems": {"totalCount": 0, "nodes": []}}})

    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    service._search_civic(variant)

    assert variant_names[0] == "R175H"
    assert "Arg175His" in variant_names


def test_civic_not_found(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def fake_post(*args, **kwargs):
        return FakeResponse({"data": {"browseMolecularProfiles": {"filteredCount": 0, "nodes": []}}})

    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    evidence = service._search_civic(variant)

    assert evidence.status == "not_found"


def test_civic_graphql_error_is_reported(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def fake_post(*args, **kwargs):
        return FakeResponse({"errors": [{"message": "schema changed"}]})

    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    evidence = service._search_civic(variant)

    assert evidence.status == "error"
    assert "schema changed" in evidence.summary


def test_cancermine_reads_cached_gene_roles(tmp_path):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    cache = tmp_path / "cancermine_collated.tsv"
    cache.write_text(
        "matching_id\trole\tcancer_id\tcancer_normalized\tgene_hugo_id\tgene_entrez_id\tgene_normalized\tcitation_count\n"
        "a\tDriver\tDOID:1\tacute myeloid leukemia\tHGNC:11998\t7157\tTP53\t42\n"
        "b\tTumor_Suppressor\tDOID:2\tbreast cancer\tHGNC:11998\t7157\tTP53\t37\n"
        "c\tOncogene\tDOID:3\tlung cancer\tHGNC:11998\t7157\tTP53\t12\n",
        encoding="utf-8",
    )
    service = DatabaseSearchService(timeout=1)
    service._cancermine_cache_path = lambda: cache

    evidence = service._search_cancermine(variant)

    assert evidence.status == "found"
    assert evidence.accession == "HGNC:11998"
    assert "entries=3" in evidence.summary
    assert "Driver:42" in evidence.summary
    assert "Tumor_Suppressor:37" in evidence.summary
    assert "acute myeloid leukemia" in evidence.summary


def test_dgidb_found_uses_graphql(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def fake_post(url, json, headers, timeout):
        assert url == "https://dgidb.org/api/graphql"
        assert json["variables"]["names"] == ["TP53"]
        return FakeResponse(
            {
                "data": {
                    "genes": {
                        "nodes": [
                            {
                                "name": "TP53",
                                "conceptId": "hgnc:11998",
                                "longName": "tumor protein p53",
                                "interactions": [
                                    {
                                        "evidenceScore": 19,
                                        "interactionTypes": [{"type": "inhibitor"}],
                                        "drug": {"name": "CISPLATIN", "approved": True, "antiNeoplastic": True},
                                        "sources": [{"sourceDbName": "CIViC"}],
                                        "publications": [{"pmid": 123}],
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        )

    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    evidence = service._search_dgidb(variant)

    assert evidence.status == "found"
    assert evidence.accession == "hgnc:11998"
    assert "interactions=1" in evidence.summary
    assert "approved=1" in evidence.summary
    assert "CISPLATIN (inhibitor) evidence=19" in evidence.summary


def test_clingen_allele_registry_found(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def fake_get(url, params, headers, timeout):
        assert url == "https://reg.clinicalgenome.org/allele"
        assert params["hgvs"] == "NM_000546.6:c.524G>A"
        return FakeResponse(
            {
                "@id": "http://reg.genome.network/allele/CA000251",
                "communityStandardTitle": ["NM_000546.6(TP53):c.524G>A (p.Arg175His)"],
                "externalRecords": {
                    "COSMIC": [{"id": "COSM10648"}],
                    "dbSNP": [{"id": "rs28934578"}],
                },
            }
        )

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_clingen_allele_registry(variant)

    assert evidence.status == "found"
    assert evidence.accession == "CA000251"
    assert "external_records=COSMIC=1, dbSNP=1" in evidence.summary
    assert "COSM10648" in evidence.summary


def test_clingen_allele_registry_invalid_query(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def fake_get(*args, **kwargs):
        return FakeResponse({"message": "Reference allele does not match."}, status_code=400)

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_clingen_allele_registry(variant)

    assert evidence.status == "invalid_query"
    assert "Reference allele does not match" in evidence.summary


def test_cbioportal_found_counts_exact_protein_change(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(timeout=1)

    def fake_get(url, headers, timeout):
        assert url.endswith("/api/genes/TP53")
        return FakeResponse({"entrezGeneId": 7157})

    def fake_post(url, params, json, headers, timeout):
        assert url.endswith("/mutations/fetch")
        assert json == {"entrezGeneIds": [7157], "sampleListId": "msk_impact_2017_all"}
        return FakeResponse(
            [
                {
                    "proteinChange": "R175H",
                    "proteinPosStart": 175,
                    "mutationType": "Missense_Mutation",
                    "studyId": "msk_impact_2017",
                },
                {"proteinChange": "R175C", "proteinPosStart": 175, "studyId": "msk_impact_2017"},
                {"proteinChange": "A138Cfs*27", "proteinPosStart": 138, "studyId": "msk_impact_2017"},
            ]
        )

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)
    monkeypatch.setattr("archer_processor.services.database_search.requests.post", fake_post)

    evidence = service._search_cbioportal(variant)

    assert evidence.status == "found"
    assert evidence.accession == "TP53 Entrez:7157"
    assert "gene_mutations=3" in evidence.summary
    assert "exact_protein_matches=1" in evidence.summary
    assert "same_protein_position=2" in evidence.summary


def test_oncokb_found_includes_info_and_levels(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(AppSettings(oncokb_api_key="token"), timeout=1)

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/api/v1/info"):
            return FakeResponse({"dataVersion": "v4.2"})
        return FakeResponse(
            {
                "oncogenic": "Oncogenic",
                "mutationEffect": {"knownEffect": "Gain-of-function"},
                "highestSensitiveLevel": "LEVEL_1",
                "highestResistanceLevel": "LEVEL_R1",
                "highestDiagnosticImplicationLevel": "LEVEL_Dx1",
                "highestPrognosticImplicationLevel": "LEVEL_Px1",
            }
        )

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_oncokb(variant)

    assert evidence.status == "found"
    assert evidence.clinical_significance == "Oncogenic"
    assert "data_version=v4.2" in evidence.summary


def test_oncokb_nested_info_version_is_normalized():
    service = DatabaseSearchService()

    assert service._oncokb_info_value(
        {"dataVersion": {"version": "v7.4", "date": "07/31/2026"}}
    ) == "v7.4"


def test_priority_manual_urls_use_current_portals():
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService()

    assert service._manual_url("MTBP", variant) == "https://mtbp.org/analyse/"
    assert service._manual_url("Franklin", variant) == (
        "https://franklin.genoox.com/clinical-db/home"
    )


def test_oncokb_unauthorized_is_reported(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(AppSettings(oncokb_api_key="bad-token"), timeout=1)

    def fake_get(*args, **kwargs):
        return FakeResponse({}, status_code=403)

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_oncokb(variant)

    assert evidence.status == "unauthorized"


class FakeResponse:
    def __init__(self, payload, status_code=200, json_error=False, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.content = b""
        self._json_error = json_error
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} Error")
            error.response = self
            raise error

    def json(self):
        if self._json_error:
            raise self.payload
        return self.payload
