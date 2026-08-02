from pathlib import Path

import openpyxl

from archer_processor.core import FilterEngine, VariantProcessor, production_rules
from archer_processor.core.models import DatabaseEvidence
from archer_processor.io import ArcherTsvReader
from archer_processor.reports import ExcelReportWriter
from archer_processor.services import DatabaseSearchService


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


def test_custom_artifact_rules_replace_default_artifact_list():
    variants = ArcherTsvReader().read(FIXTURE)
    rules = production_rules(
        [
            {
                "gene": "TP53",
                "hgvsc": "NM_000546.6:c.524G>A",
                "reason": "Temporary local artifact for testing.",
            }
        ]
    )
    FilterEngine(rules).apply(variants)

    by_sample = {variant.patient_id: variant for variant in variants}
    assert by_sample["26OUM00001"].decision == "included"
    assert by_sample["26OUM00004"].decision == "excluded"
    assert "artifact" in by_sample["26OUM00004"].matched_rules[0]


def test_processor_writes_excel(tmp_path):
    output = tmp_path / "review.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-07-26", output)
    ExcelReportWriter().write(result, output)

    assert output.exists()
    assert result.total_count == 5
    assert len(result.included) == 3
    assert len(result.excluded) == 2


def test_excel_export_preserves_raw_columns_and_adds_database_columns(tmp_path):
    output = tmp_path / "review.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-07-26", output)
    variant = result.variants[3]
    evidence = {
        DatabaseSearchService().variant_key(variant): [
            DatabaseEvidence("ClinVar", "found", "ClinVar summary"),
            DatabaseEvidence("gnomAD", "found", "gnomAD summary"),
            DatabaseEvidence("COSMIC", "found", "COSMIC summary"),
            DatabaseEvidence("CIViC", "found", "CIViC summary"),
            DatabaseEvidence("CancerMine", "found", "CancerMine summary"),
            DatabaseEvidence("DGIdb", "found", "DGIdb summary"),
            DatabaseEvidence("ClinGen Allele Registry", "found", "ClinGen summary"),
            DatabaseEvidence("cBioPortal", "found", "cBioPortal summary"),
            DatabaseEvidence("OncoKB", "token_required", "OncoKB token required"),
            DatabaseEvidence("Franklin", "token_required", "Franklin token required"),
            DatabaseEvidence("MTBP", "manual", "MTBP manual"),
            DatabaseEvidence("HSMD", "manual", "HSMD manual"),
        ]
    }

    ExcelReportWriter().write(result, output, evidence=evidence)

    workbook = openpyxl.load_workbook(output)
    assert workbook.sheetnames == [
        "Summary",
        "Included Variants",
        "Database Evidence",
        "With Artifacts",
        "Artifacts Removed",
        "Rules",
    ]
    ws = workbook["With Artifacts"]
    headers = [cell.value for cell in ws[1]]
    raw_headers = list(result.variants[0].raw)
    row = next(row for row in ws.iter_rows(min_row=2, values_only=True) if row[0] == variant.sample)
    workbook.close()

    assert headers[: len(raw_headers)] == raw_headers
    assert headers[len(raw_headers):] == [
        f"{database} Evidence"
        for database in [
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
        ]
    ]
    assert row[headers.index("HGVSc")] == variant.raw["HGVSc"]
    assert row[headers.index("CIViC Evidence")] == "[found] CIViC summary"


def test_excel_export_writes_artifact_removed_sheet(tmp_path):
    output = tmp_path / "review.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-07-26", output)
    variant = result.variants[3]
    evidence = {
        DatabaseSearchService().variant_key(variant): [
            DatabaseEvidence("ClinVar", "found", "ClinVar summary"),
            DatabaseEvidence("gnomAD", "found", "gnomAD summary"),
            DatabaseEvidence("COSMIC", "found", "COSMIC summary"),
            DatabaseEvidence("CIViC", "found", "CIViC summary"),
            DatabaseEvidence("CancerMine", "found", "CancerMine summary"),
            DatabaseEvidence("DGIdb", "found", "DGIdb summary"),
            DatabaseEvidence("ClinGen Allele Registry", "found", "ClinGen summary"),
            DatabaseEvidence("cBioPortal", "found", "cBioPortal summary"),
            DatabaseEvidence("OncoKB", "token_required", "OncoKB token required"),
            DatabaseEvidence("Franklin", "unauthorized", "Franklin login was rejected"),
            DatabaseEvidence("MTBP", "manual", "MTBP manual"),
            DatabaseEvidence("HSMD", "manual", "HSMD manual"),
        ]
    }

    ExcelReportWriter().write(result, output, evidence=evidence)

    workbook = openpyxl.load_workbook(output)
    with_artifacts = workbook["With Artifacts"]
    artifacts_removed = workbook["Artifacts Removed"]
    with_samples = [row[0] for row in with_artifacts.iter_rows(min_row=2, values_only=True)]
    removed_samples = [row[0] for row in artifacts_removed.iter_rows(min_row=2, values_only=True)]
    headers = [cell.value for cell in artifacts_removed[1]]
    row = next(row for row in artifacts_removed.iter_rows(min_row=2, values_only=True) if row[0] == variant.sample)
    workbook.close()

    assert "26OUM00001_VPM_S1_R1_001" in with_samples
    assert "26OUM00001_VPM_S1_R1_001" not in removed_samples
    assert row[headers.index("ClinVar Evidence")] == "[found] ClinVar summary"
    assert row[headers.index("Franklin Evidence")] == "[unauthorized] Franklin login was rejected"


def test_excel_export_keeps_row_coloring_on_raw_sheets(tmp_path):
    output = tmp_path / "review.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-07-26", output)
    by_patient = {variant.patient_id: variant for variant in result.variants}
    by_patient["26OUM00004"].history_matches = [{"Tier I": 2, "Tier II": 3}]
    by_patient["26OUM00005"].history_matches = [{"Germ": 10}]

    ExcelReportWriter().write(result, output)

    workbook = openpyxl.load_workbook(output)
    with_artifacts = workbook["With Artifacts"]
    artifacts_removed = workbook["Artifacts Removed"]
    by_sample = {
        row[0].value: row[0].fill.fgColor.rgb
        for row in with_artifacts.iter_rows(min_row=2)
    }
    removed_by_sample = {
        row[0].value: row[0].fill.fgColor.rgb
        for row in artifacts_removed.iter_rows(min_row=2)
    }
    workbook.close()

    assert by_sample["26OUM00001_VPM_S1_R1_001"] == "00FCE4D6"
    assert by_sample["26OUM00002_VPM_S2_R1_001"] == "00000000"
    assert removed_by_sample["26OUM00004_VPM_S4_R1_001"] == "00FFF2CC"
    assert removed_by_sample["26OUM00005_VPM_S5_R1_001"] == "00E2F0D9"


def test_excel_evidence_sheet_links_source_and_browser_screenshot(tmp_path):
    output = tmp_path / "review.xlsx"
    screenshot = tmp_path / "browser-evidence.png"
    screenshot.write_bytes(b"placeholder")
    result = VariantProcessor().process(FIXTURE, "2026-07-26", output)
    variant = result.variants[3]
    evidence = {
        DatabaseSearchService().variant_key(variant): [
            DatabaseEvidence(
                "OncoKB",
                "found",
                "oncogenic=Oncogenic",
                clinical_significance="Oncogenic",
                url="https://www.oncokb.org/example",
                raw={
                    "screenshot": str(screenshot),
                    "captured_at": "2026-08-01T12:00:00+00:00",
                },
            )
        ]
    }

    ExcelReportWriter().write(result, output, evidence=evidence)

    workbook = openpyxl.load_workbook(output)
    ws = workbook["Database Evidence"]
    headers = [cell.value for cell in ws[1]]
    source_cell = ws.cell(2, headers.index("Source Page") + 1)
    screenshot_cell = ws.cell(2, headers.index("Screenshot") + 1)
    workbook.close()

    assert source_cell.hyperlink.target == "https://www.oncokb.org/example"
    assert screenshot_cell.hyperlink.target == "browser-evidence.png"
