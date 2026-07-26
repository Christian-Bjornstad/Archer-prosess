from pathlib import Path

import openpyxl

from archer_processor.core import VariantProcessor
from archer_processor.knowledge import VariantHistoryRepository


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


def test_history_repository_annotates_variants(tmp_path):
    workbook_path = tmp_path / "history.xlsx"
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = "RESULTAT"
    ws.append(["Sample", "Classification", "Symbol", "HGVSc", "TO", "Rept", "Germ", "Artf"])
    ws.append(["OLD_SAMPLE", "VUS", "EZH2", "NM_004456.4:c.118-4dup", 3, 1, 0, 0])
    workbook.save(workbook_path)

    history = VariantHistoryRepository(workbook_path)
    result = VariantProcessor(history=history).process(FIXTURE, "2026-07-26")
    ezh2 = next(variant for variant in result.variants if variant.symbol == "EZH2")

    assert len(ezh2.history_matches) == 1
    assert history.stats()["total_entries"] == 1
