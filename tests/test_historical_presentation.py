from dataclasses import replace
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl import Workbook
from pypdf import PdfReader

from archer_processor.core.models import DatabaseEvidence, ProcessingResult, VariantRecord
from archer_processor.core.evidence_display import historical_timestamp
from archer_processor.gui.status_model import CellState, build_patient_status_rows, cell_state_for_evidence
from archer_processor.gui.widgets.status_matrix import StatusMatrix
from archer_processor.reports.excel_report import ExcelReportWriter
from archer_processor.reports.patient_excel import PatientExcelReportWriter
from archer_processor.reports.patient_pdf import PatientPdfReportWriter


ORIGINAL_CAPTURE = "2025-03-14T09:26:00+00:00"


def historical(database="ClinVar", captured_at=ORIGINAL_CAPTURE):
    return DatabaseEvidence(
        database, "historical", "classification=Pathogenic", clinical_significance="Pathogenic",
        raw={
            "captured_at": captured_at,
            "historical_result": {
                "captured_at": captured_at,
                "used_at": "2026-10-15T11:00:00+00:00",
                "current_attempt": {"status": "timeout", "summary": "Result table never rendered"},
            },
        },
    )


def variant(change="c.1A>G"):
    return VariantRecord(Path("synthetic.tsv"), 2, "SYNTHETIC01", "TESTGENE", change, cosmic_id="COSM999999")


def local_timestamp(value=ORIGINAL_CAPTURE):
    local = datetime.fromisoformat(value).astimezone()
    offset = local.strftime("%z")
    return f"{local:%d.%m.%Y kl. %H:%M} (UTC{offset[:3]}:{offset[3:]})"


def patient_rows(items, variants=None, **kwargs):
    variants = variants or [variant()]
    return build_patient_status_rows(
        variants, databases=["ClinVar"],
        evidence={f"{v.sample}|{v.hgvsc}": [item] for v, item in zip(variants, items)},
        skipped_keys=set(), report_outcomes={}, **kwargs,
    )


def test_historical_status_is_distinct_from_fresh_completion():
    assert cell_state_for_evidence(historical()).value == "historical"


def test_matrix_exposes_original_timestamp_and_current_failure_without_hover(qt_app):
    rows = patient_rows([historical()])
    matrix = StatusMatrix(["ClinVar"])
    matrix.set_rows(rows)
    cell = matrix.item(0, 2)
    assert "Tidligere resultat" in cell.text()
    assert local_timestamp() in cell.text()
    assert "2026-10-15" not in cell.text()
    assert "Result table never rendered" in rows[0].cells["ClinVar"].detail
    assert matrix.rowHeight(0) >= matrix.fontMetrics().height() * 2
    assert cell.background().color().name() != "#e9f6ef"


def test_matrix_shows_oldest_history_and_individual_dates_when_aggregating():
    rows = patient_rows(
        [historical(), historical(captured_at="2026-09-01T12:00:00+00:00")],
        variants=[variant(), variant("c.2A>G")],
    )
    cell = rows[0].cells["ClinVar"]
    assert local_timestamp() in cell.label
    assert local_timestamp("2026-09-01T12:00:00+00:00") in cell.detail


def test_historical_lookup_becomes_running_when_actively_retried():
    rows = patient_rows([historical()], active={("SYNTHETIC01", "ClinVar")})
    assert rows[0].cells["ClinVar"].state is CellState.RUNNING


def test_historical_report_cells_mark_old_classification_and_original_time():
    item = historical()
    writer = PatientExcelReportWriter()
    for text in (writer._compact_evidence([item]), writer._text_evidence([item]), writer._status_text([item]), ExcelReportWriter()._evidence_cell([item])):
        assert "Tidligere resultat" in text
        assert local_timestamp() in text
    assert "Pathogenic" in writer._compact_evidence([item])
    assert ExcelReportWriter()._evidence_cell([item]).startswith("[historical]")


def test_missing_history_timestamp_remains_explicitly_unknown():
    item = historical(captured_at="")
    text = PatientExcelReportWriter()._compact_evidence([item])
    assert "Tidligere resultat" in text
    assert "ukjent tidspunkt" in text.lower()


def test_naive_or_invalid_history_times_never_invent_a_timezone():
    for invalid in ("2025-03-14T09:26:00", "not a date"):
        assert historical_timestamp(historical(captured_at=invalid)) == "ukjent tidspunkt"


def test_history_timestamp_accepts_utc_z_suffix():
    assert historical_timestamp(historical(captured_at="2025-03-14T09:26:00Z")) == local_timestamp()


def test_unknown_history_age_is_not_hidden_by_a_known_capture_date():
    rows = patient_rows([historical(), historical(captured_at="")], variants=[variant(), variant("c.2A>G")])
    assert "ukjent tidspunkt" in rows[0].cells["ClinVar"].label


def test_patient_workbook_marks_history_in_classification_and_cosmic_cells(tmp_path):
    v = variant()
    result = ProcessingResult(Path("synthetic.tsv"), None, "2026-10-15", [v], [])
    evidence = {f"{v.sample}|{v.hgvsc}": [historical(), historical("COSMIC")]}
    output = tmp_path / "patient.xlsx"
    PatientExcelReportWriter().write_patient(result, v.patient_id, [v], output, evidence)
    workbook = openpyxl.load_workbook(output)
    try:
        overview = workbook["Oversikt"]
        for coordinate in ("D8", "H8", "J8"):
            assert "Tidligere resultat" in overview[coordinate].value
            assert local_timestamp() in overview[coordinate].value
        assert "COSM999999" in overview["J8"].value
    finally:
        workbook.close()


def test_optional_evidence_data_sheet_keeps_original_capture_time():
    v = variant()
    workbook = Workbook()
    PatientExcelReportWriter()._evidence_data_sheet(
        workbook, v.patient_id, [v], {f"{v.sample}|{v.hgvsc}": [historical()]}
    )
    data = workbook["Evidence Data"]
    assert "Tidligere resultat" in data["F2"].value
    assert local_timestamp() in data["F2"].value
    assert data["K2"].value == ORIGINAL_CAPTURE


def test_pdf_marks_history_and_requires_followup(tmp_path):
    v = variant()
    result = ProcessingResult(Path("synthetic.tsv"), None, "2026-10-15", [v], [])
    output = tmp_path / "patient.pdf"
    PatientPdfReportWriter().write_patient(v.patient_id, [v], [v], result, output, {f"{v.sample}|{v.hgvsc}": [historical()]})
    text = " ".join(" ".join((page.extract_text() or "").split()) for page in PdfReader(output).pages)
    assert "Tidligere resultat" in text
    assert local_timestamp() in text
    assert "1 evidence record(s) requiring follow-up" in text


def test_historical_classification_does_not_enter_fresh_conflict_summary():
    current = DatabaseEvidence("Franklin", "found", clinical_significance="Benign")
    paragraphs = PatientPdfReportWriter()._review_section([variant()], [historical(), current])
    assert not any("Different classifications" in paragraph.getPlainText() for paragraph in paragraphs)


def test_fresh_classification_presentation_is_unchanged():
    item = replace(historical(), status="found", raw={})
    assert PatientExcelReportWriter()._compact_evidence([item]) == "Pathogenic"


def test_historical_screenshot_caption_keeps_original_date_visible_on_one_line():
    item = historical()
    item.raw["screenshots"] = [{"path": "synthetic.png", "label": "Variant overview"}]
    caption = PatientExcelReportWriter()._screenshot_records([item])[0]["label"]
    assert "Tidligere resultat" in caption
    assert local_timestamp() in caption
    assert "\n" not in caption
