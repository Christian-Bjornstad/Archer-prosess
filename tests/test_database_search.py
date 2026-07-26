from pathlib import Path

from archer_processor.core.models import DatabaseEvidence
from archer_processor.io import ArcherTsvReader
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


def test_franklin_without_token_prepares_search_query():
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = DatabaseSearchService()

    evidence = service._search_franklin(variant)

    assert evidence.status == "token_required"
    assert "Query prepared: chr13-28608215-C-CT" in evidence.summary


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
