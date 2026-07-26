from pathlib import Path

from archer_processor.core import FilterEngine, VariantProcessor
from archer_processor.io import ArcherTsvReader
from archer_processor.reports import ExcelReportWriter


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


def test_reader_parses_archer_tsv_columns():
    variants = ArcherTsvReader().read(FIXTURE)

    assert len(variants) == 5
    assert variants[0].sample == "26OUM00001_VPM_S1_R1_001"
    assert variants[0].patient_id == "26OUM00001"
    assert variants[0].transcript == "NM_004119.2"
    assert variants[3].af == 0.5333


def test_production_rules_and_boundaries():
    variants = ArcherTsvReader().read(FIXTURE)
    FilterEngine().apply(variants)

    by_sample = {variant.patient_id: variant for variant in variants}
    assert by_sample["26OUM00001"].decision == "excluded"
    assert by_sample["26OUM00002"].decision == "excluded"
    assert by_sample["26OUM00003"].decision == "included"
    assert by_sample["26OUM00004"].warnings
    assert by_sample["26OUM00005"].warnings


def test_processor_writes_excel(tmp_path):
    output = tmp_path / "review.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-07-26", output)
    ExcelReportWriter().write(result, output)

    assert output.exists()
    assert result.total_count == 5
    assert len(result.included) == 3
    assert len(result.excluded) == 2
