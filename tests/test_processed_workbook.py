import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import openpyxl
import pytest

from archer_processor.core import DatabaseEvidence, VariantProcessor
from archer_processor.reports import ExcelReportWriter
from archer_processor.services import ProcessedWorkbookLoader


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


def test_processed_workbook_restores_variants_x_marks_and_evidence(tmp_path):
    output = tmp_path / "restored_review.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-08-05", output)
    variant = result.variants[3]
    key = f"{variant.sample}|{variant.hgvsc}"
    evidence = {
        key: [
            DatabaseEvidence("ClinVar", "found", "Benign"),
            DatabaseEvidence(
                "OncoKB",
                "found",
                "oncogenic=Likely Oncogenic",
                accession="G646Wfs*12",
                clinical_significance="Likely Oncogenic",
                url="https://www.oncokb.org/example",
                raw={"screenshot": str(tmp_path / "oncokb.png")},
            ),
        ]
    }
    manual_skip = f"{result.variants[1].sample}|{result.variants[1].hgvsc}"
    ExcelReportWriter().write(
        result, output, evidence=evidence, database_skip_keys={manual_skip}
    )

    digest = hashlib.sha256(
        f"OncoKB|{variant.symbol}|{variant.hgvsc}|{variant.hgvsp}".encode("utf-8")
    ).hexdigest()[:16]
    audit = (
        tmp_path
        / "restored_review_browser_evidence"
        / "patient-004"
        / "oncokb"
        / f"{digest}.audit.json"
    )
    audit.parent.mkdir(parents=True)
    audit.write_text(json.dumps(asdict(evidence[key][1])), encoding="utf-8")

    state = ProcessedWorkbookLoader().load(output)

    assert state.result.output_path == output.resolve()
    assert state.result.total_count == result.total_count
    assert manual_skip in state.database_skip_keys
    assert f"{result.variants[0].sample}|{result.variants[0].hgvsc}" in state.database_skip_keys
    restored = {item.database: item for item in state.evidence[key]}
    assert restored["ClinVar"].summary == "Benign"
    assert restored["OncoKB"].clinical_significance == "Likely Oncogenic"
    assert restored["OncoKB"].url == "https://www.oncokb.org/example"
    assert restored["OncoKB"].raw["screenshot"].endswith("oncokb.png")


def test_processed_workbook_rejects_unrelated_excel_file(tmp_path):
    path = tmp_path / "unrelated.xlsx"
    workbook = openpyxl.Workbook()
    workbook.save(path)

    with pytest.raises(ValueError, match="With Artifacts"):
        ProcessedWorkbookLoader().load(path)


def test_processed_workbook_preserves_x_marked_row_without_hgvsc(tmp_path):
    output = tmp_path / "partial_annotation.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-08-05", output)
    result.variants[-1].hgvsc = ""
    result.variants[-1].raw["HGVSc"] = ""
    key = f"{result.variants[-1].sample}|"
    ExcelReportWriter().write(result, output, database_skip_keys={key})

    state = ProcessedWorkbookLoader().load(output)

    assert state.result.total_count == result.total_count
    assert state.result.variants[-1].hgvsc == ""
    assert key in state.database_skip_keys
