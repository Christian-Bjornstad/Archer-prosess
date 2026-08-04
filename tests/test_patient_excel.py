from pathlib import Path

import openpyxl
from PIL import Image

from archer_processor.core import DatabaseEvidence, VariantProcessor
from archer_processor.reports import PatientExcelReportWriter
from archer_processor.services import DatabaseSearchService


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


def test_patient_excel_report_uses_one_image_led_sheet(tmp_path):
    result = VariantProcessor().process(
        FIXTURE, "2026-08-03", tmp_path / "review.xlsx"
    )
    variant = result.variants[3]
    image_paths = []
    for name, size, color in [
        ("franklin-full.png", (1400, 2000), "#D9EAF7"),
        ("franklin-assessment.png", (1400, 500), "#EAF3FA"),
        ("oncokb.png", (1400, 700), "#EAF5ED"),
        ("mtbp.png", (1200, 600), "#FFF3E8"),
    ]:
        path = tmp_path / name
        Image.new("RGB", size, color).save(path)
        image_paths.append(path)

    evidence = {
        DatabaseSearchService().variant_key(variant): [
            DatabaseEvidence(
                "ClinVar",
                "found",
                "Pathogenic with expert-panel review.",
                accession="VCV000012345.1",
                clinical_significance="Pathogenic",
                url="https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/",
            ),
            DatabaseEvidence(
                "gnomAD",
                "found",
                "aggregated_AF=0.001%; homozygotes=0",
                accession="17-7578406-G-A",
                url="https://gnomad.broadinstitute.org/variant/example",
            ),
            DatabaseEvidence(
                "COSMIC",
                "found",
                "COSMIC public dataset: 2 records returned; primary_sites=haematopoietic_and_lymphoid_tissue",
                accession="20479064",
                url="https://cancer.sanger.ac.uk/cosmic/search?q=TP53",
                raw={
                    "query": "TP53 c.524G>A",
                    "records": [
                        {
                            "GeneName": "TP53",
                            "MutationCDS": "c.524G>A",
                            "MutationAA": "p.R175H",
                            "MutationID": "20479064",
                            "LegacyMutationID": "COSM99023",
                            "GenomicMutationID": "COSV52661038",
                            "PrimarySite": "haematopoietic_and_lymphoid_tissue",
                            "PrimaryHistology": "haematopoietic_neoplasm",
                            "PubmedPMID": "12345; 67890",
                            "GRChVer": "37",
                        },
                        {
                            "GeneName": "TP53",
                            "MutationCDS": "c.524G>A",
                            "MutationAA": "p.R175H",
                            "MutationID": "20479064",
                            "PrimarySite": "lung",
                            "PrimaryHistology": "carcinoma",
                            "PubmedPMID": "24680",
                            "GRChVer": "37",
                        },
                    ],
                },
            ),
            DatabaseEvidence(
                "Franklin",
                "found",
                "classification=Pathogenic",
                clinical_significance="Pathogenic",
                url="https://franklin.genoox.com/example",
                raw={
                    "screenshots": [
                        {
                            "label": "Full computed-classification page",
                            "path": str(image_paths[0]),
                            "url": "https://franklin.genoox.com/example",
                        },
                        {
                            "label": "Predictions and population frequencies",
                            "path": str(image_paths[1]),
                            "url": "https://franklin.genoox.com/example?app=assessment-tools",
                        },
                    ]
                },
            ),
            DatabaseEvidence(
                "OncoKB",
                "found",
                "oncogenic=Oncogenic",
                url="https://www.oncokb.org/example",
                raw={
                    "screenshots": [
                        {
                            "label": "Variant overview and mutation effect",
                            "path": str(image_paths[2]),
                            "url": "https://www.oncokb.org/example",
                        }
                    ]
                },
            ),
            DatabaseEvidence(
                "MTBP",
                "found",
                "Putative functionally relevant variant",
                url="https://mtbp.org/example",
                raw={
                    "screenshots": [
                        {
                            "label": "Alteration-centric functional evidence",
                            "path": str(image_paths[3]),
                            "url": "https://mtbp.org/example",
                        }
                    ]
                },
            ),
        ]
    }
    output = tmp_path / "patient.xlsx"

    PatientExcelReportWriter().write_patient(
        result,
        variant.patient_id,
        [variant],
        output,
        evidence,
    )

    workbook = openpyxl.load_workbook(output)
    assert workbook.sheetnames == ["Report"]
    report = workbook["Report"]
    assert report["A1"].value == "Patient ID: 26OUM00004"
    assert report["A3"].value == "TP53 | NM_000546.6:c.524G>A | p.R175H"
    assert report["A4"].value == "ClinVar"
    assert report["A5"].value == "gnomAD"
    assert report["A6"].value == "COSMIC"
    assert len(report._images) == 4
    workbook.close()
