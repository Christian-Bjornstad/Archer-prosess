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

    diagnostics = service.database_diagnostics(["ClinVar", "gnomAD", "COSMIC", "OncoKB", "Franklin", "MTBP", "HSMD"])

    assert diagnostics["ClinVar"] == "ready"
    assert diagnostics["gnomAD"].startswith("ready")
    assert diagnostics["COSMIC"] == "ready (basic/public lookup)"
    assert diagnostics["OncoKB"] == "token required"
    assert diagnostics["Franklin"] == "token required"
    assert diagnostics["MTBP"] == "manual"
    assert diagnostics["HSMD"] == "manual"


def test_database_diagnostics_reports_franklin_login_ready():
    settings = AppSettings(franklin_api_key="", franklin_email="user@example.org", franklin_password="secret")
    service = DatabaseSearchService(settings)

    diagnostics = service.database_diagnostics(["Franklin"])

    assert diagnostics["Franklin"] == "ready (login on search)"


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


def test_franklin_without_token_prepares_search_query():
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService()

    evidence = service._search_franklin(variant)

    assert evidence.status == "token_required"
    assert "Query prepared: chr13-28608215-C-CT" in evidence.summary


def test_franklin_login_fetches_token_and_searches(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService(
        AppSettings(franklin_api_key="", franklin_email="user@example.org", franklin_password="secret"),
        timeout=1,
    )
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params or {}, headers or {}))
        if url.endswith("/v1/auth/login"):
            assert params["email"] == "user@example.org"
            assert headers["Authorization"] == "secret"
            return FakeResponse({"data": {"token": "session-token"}})
        assert url.endswith("/v2/search/snp/")
        assert headers["Authorization"] == "Bearer session-token"
        return FakeResponse(
            {
                "variants": [
                    {
                        "classification": {"acmg_classification": "uncertain significance"},
                        "annotations": {},
                    }
                ]
            }
        )

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_franklin(variant)

    assert evidence.status == "found"
    assert evidence.clinical_significance == "uncertain significance"
    assert [call[0] for call in calls] == [
        "https://api.genoox.com/v1/auth/login",
        "https://api.genoox.com/v2/search/snp/",
    ]


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
    assert "COSMIC basic/public lookup" in evidence.summary
    assert evidence.accession == "10648"


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


def test_oncokb_unauthorized_is_reported(monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    service = DatabaseSearchService(AppSettings(oncokb_api_key="bad-token"), timeout=1)

    def fake_get(*args, **kwargs):
        return FakeResponse({}, status_code=403)

    monkeypatch.setattr("archer_processor.services.database_search.requests.get", fake_get)

    evidence = service._search_oncokb(variant)

    assert evidence.status == "unauthorized"


class FakeResponse:
    def __init__(self, payload, status_code=200, json_error=False):
        self.payload = payload
        self.status_code = status_code
        self.content = b""
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} Error")
            error.response = self
            raise error

    def json(self):
        if self._json_error:
            raise self.payload
        return self.payload
