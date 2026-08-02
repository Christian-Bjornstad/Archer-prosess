from pathlib import Path

from pypdf import PdfReader

from archer_processor.core import VariantProcessor
from archer_processor.core.models import DatabaseEvidence
from archer_processor.reports import DIT_PATTERN, PatientPdfReportWriter


FIXTURE = Path(__file__).parent / "fixtures" / "sample_variants.tsv"


def test_patient_pdf_writer_groups_included_variants_by_dit(tmp_path):
    result = VariantProcessor().process(
        FIXTURE, "2026-08-01", tmp_path / "review.xlsx"
    )
    tp53 = next(variant for variant in result.included if variant.symbol == "TP53")
    evidence = {
        f"{tp53.sample}|{tp53.hgvsc}": [
            DatabaseEvidence(
                "OncoKB",
                "found",
                "oncogenic=Oncogenic; mutation_effect=Gain-of-function",
                accession="TP53 R175H",
                clinical_significance="Oncogenic",
                url="https://www.oncokb.org/gene/TP53/somatic/R175H",
                raw={"captured_at": "2026-08-01T12:00:00+00:00"},
            )
        ]
    }

    outputs = PatientPdfReportWriter().write_all(
        result, tmp_path / "patient-reports", evidence
    )

    assert {path.name for path in outputs} == {
        "26OUM00003_variant_review_2026-08-01.pdf",
        "26OUM00004_variant_review_2026-08-01.pdf",
        "26OUM00005_variant_review_2026-08-01.pdf",
    }
    assert not list((tmp_path / "patient-reports").glob("26OUM00001*.pdf"))
    tp53_pdf = tmp_path / "patient-reports" / "26OUM00004_variant_review_2026-08-01.pdf"
    reader = PdfReader(tp53_pdf)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    uris = []
    for page in reader.pages:
        for annotation_ref in page.get("/Annots", []):
            annotation = annotation_ref.get_object()
            action = annotation.get("/A") or {}
            if action.get("/URI"):
                uris.append(str(action["/URI"]))

    assert len(reader.pages) >= 2
    assert "DIT identifier" in text
    assert "26OUM00004" in text
    assert "TP53" in text
    assert "NM_000546.6:c.524G>A" in text
    assert "Oncogenic" in text
    assert "Responsible physician conclusion" in text
    assert "does not establish a diagnosis" in text
    assert "https://www.oncokb.org/gene/TP53/somatic/R175H" in uris


def test_dit_format_and_year_validation():
    writer = PatientPdfReportWriter()

    assert DIT_PATTERN.fullmatch("26OUM00000")
    assert not DIT_PATTERN.fullmatch("OUM00000")
    assert writer._dit_validation_message("26OUM00000", "2026-08-01") == ""
    assert "differs from report year" in writer._dit_validation_message(
        "25OUM00000", "2026-08-01"
    )
