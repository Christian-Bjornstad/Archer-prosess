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
