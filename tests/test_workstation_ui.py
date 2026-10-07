from pathlib import Path
import time

import pytest
from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent

from archer_processor.core import DatabaseEvidence
from archer_processor.core.models import ProcessingResult, VariantRecord
from archer_processor.gui.app import MainWindow
from archer_processor.gui.app import DatabaseWorker
from archer_processor.gui.status_model import CellState, build_patient_status_rows
from archer_processor.services.settings import AppSettings
from archer_processor.gui.widgets.file_drop import AnalysisFileDrop


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(AppSettings, "load", classmethod(lambda cls: cls(default_output_dir=str(tmp_path))))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    value = MainWindow()
    yield value
    value.close()


def variant():
    return VariantRecord(Path("synthetic.tsv"), 2, "DEMO_VPM_A", "TP53", "c.524G>A")


def test_unselected_source_is_not_presented_as_queued():
    item = variant()
    rows = build_patient_status_rows(
        [item], databases=["ClinVar", "Franklin"], evidence={}, skipped_keys=set(),
        report_outcomes={}, selected_sources={"ClinVar"},
    )
    assert rows[0].cells["ClinVar"].state is CellState.QUEUED
    assert rows[0].cells["Franklin"].label == "Not selected"
    assert "not selected" in rows[0].cells["Franklin"].detail.casefold()


def test_unselecting_source_keeps_collected_evidence_visible():
    item = variant()
    rows = build_patient_status_rows(
        [item], databases=["ClinVar"],
        evidence={"DEMO_VPM_A|c.524G>A": [DatabaseEvidence("ClinVar", "not_found", "No exact record")]},
        skipped_keys=set(), report_outcomes={}, selected_sources=set(),
    )
    assert rows[0].cells["ClinVar"].state is CellState.NOT_FOUND


def test_browser_readiness_and_queue_inputs_locked_during_search(window):
    window._set_busy("Searching")
    assert not window.check_sessions_btn.isEnabled()
    assert not window.validate_btn.isEnabled()
    assert not window.load_selection_btn.isEnabled()
    assert not window.included_only_check.isEnabled()
    assert all(not check.isEnabled() for check in window.db_checks.values())
    window.input_edit.setText(".")
    window.output_edit.setText("review.xlsx")
    assert not window.process_btn.isEnabled()
    window._set_ready()
    assert window.check_sessions_btn.isEnabled()
    assert window.validate_btn.isEnabled()
    assert window.included_only_check.isEnabled()


def test_local_tsv_selection_sets_output_without_starting_work(window, tmp_path):
    input_path = tmp_path / "variants.tsv"
    input_path.write_text("synthetic only", encoding="utf-8")
    window._open_analysis_file(str(input_path))
    assert window.input_edit.text() == str(input_path)
    assert Path(window.output_edit.text()) == tmp_path / "variants_VPM_review.xlsx"
    assert window.processing_thread is None
    assert window.tabs.currentIndex() == 0
    assert window.process_btn.isEnabled()


def test_file_selection_ignored_while_operation_owns_analysis(window, tmp_path):
    input_path = tmp_path / "variants.tsv"
    input_path.write_text("synthetic only", encoding="utf-8")
    window._set_busy("Searching")
    window._open_analysis_file(str(input_path))
    assert window.input_edit.text() == ""
    assert window.processing_thread is None


def test_input_must_be_file_not_existing_directory(window, tmp_path):
    window.input_edit.setText(str(tmp_path))
    window.output_edit.setText(str(tmp_path / "review.xlsx"))
    assert not window.process_btn.isEnabled()


def test_small_workstation_evidence_controls_fit(window, qt_app):
    window.resize(1024, 640)
    window._switch_page(1)
    window.show()
    qt_app.processEvents()
    assert window.width() == 1024
    assert window.height() == 640
    viewport = window.database_scroll.viewport()
    for control in (window.search_btn, window.check_sessions_btn, window.priority_search_btn,
                    window.remaining_search_btn, window.patient_excel_btn, window.rewrite_btn):
        point = control.mapTo(viewport, control.rect().topLeft())
        assert point.x() >= 0
        assert point.x() + control.width() <= viewport.width()


def test_live_source_rows_follow_selection(window):
    item = variant()
    window.result = ProcessingResult(Path("synthetic.tsv"), None, "2026-10-07", [item], [])
    window.db_checks["Franklin"].setChecked(False)
    window._refresh_operations_cockpit()
    column = window.databases.index("Franklin") + 2
    assert window.status_matrix.item(0, column).text() == "Not selected"


def test_drop_routes_one_local_workbook_and_rejects_multiple_files(qt_app, tmp_path):
    widget = AnalysisFileDrop()
    files = []
    widget.file_selected.connect(files.append)
    workbook = tmp_path / "review.xlsx"
    workbook.touch()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(workbook))])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)
    assert files == [str(workbook)]
    assert event.isAccepted()
    mime.setUrls([QUrl.fromLocalFile(str(workbook)), QUrl.fromLocalFile(str(workbook))])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)
    assert files == [str(workbook)]
    assert not event.isAccepted()


def test_session_check_updates_individual_results_and_unlocks_ui(window, qt_app, monkeypatch):
    from archer_processor.services import browser_sessions
    from archer_processor.services.browser_sessions import BrowserSessionStatus

    class Checker:
        def __init__(self, review):
            pass

        def check_all(self, *, on_result):
            results = [BrowserSessionStatus(name, "public" if name == "ClinVar" else "login_required", "Synthetic check")
                       for name in window.databases]
            for item in results:
                on_result(item)
            return results

    monkeypatch.setattr(browser_sessions, "BrowserSessionCheckService", Checker)
    window._start_session_check()
    assert not window.check_sessions_btn.isEnabled()
    deadline = time.monotonic() + 3
    while window._operation_active and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not window._operation_active
    assert window.check_sessions_btn.isEnabled()
    assert window.session_status_labels["ClinVar"].text() == "Offentlig tilgang"
    assert window.session_status_labels["Franklin"].text() == "Krever innlogging"
    assert "1/5" in window.session_check_summary.text()
    assert "Sjekket" in window.session_status_labels["ClinVar"].toolTip()
    # Let the worker thread's queued quit/delete callbacks finish before teardown.
    for _ in range(10):
        qt_app.processEvents()
        time.sleep(0.005)


def test_session_check_cannot_start_during_active_search(window):
    window._set_busy("Searching")
    window._start_session_check()
    assert window.session_check_thread is None
    assert window._operation_active


def test_new_analysis_clears_previous_report_status(window, monkeypatch, tmp_path):
    from archer_processor.reports.patient_report_coordinator import PatientReportOutcome
    from PyQt6.QtWidgets import QMessageBox

    window.report_outcomes["DEMO"] = PatientReportOutcome("DEMO", tmp_path / "report.xlsx", "created", "Synthetic report")
    item = variant()
    window.result = ProcessingResult(Path("old-synthetic.tsv"), None, "2026-10-06", [item], [])
    window._refresh_operations_cockpit()
    window.status_matrix.selectRow(0)
    assert window.status_matrix.selectionModel().selectedRows()
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(window, "_remember_recent_workbook", lambda path: None)
    result = ProcessingResult(Path("synthetic.tsv"), None, "2026-10-07", [item], [])
    window._processing_finished(result)
    assert window.report_outcomes == {}
    assert not window.status_matrix.selectionModel().selectedRows()
    assert window.continue_evidence_btn.isEnabled()
    assert window.status_matrix.item(0, window.status_matrix.columnCount() - 1).text() == "Not ready"


def test_main_worker_updates_variants_live_but_checkpoints_patient_once(qt_app, monkeypatch, tmp_path):
    first = variant()
    second = VariantRecord(Path("synthetic.tsv"), 3, "DEMO_VPM_A", "TP53", "c.743G>A")
    worker = DatabaseWorker([first, second], [], ["ClinVar"], tmp_path,
                            AppSettings(browser_delay_seconds=0, browser_delay_max_seconds=0))
    updates, checkpoints = [], []
    worker.evidence_updated.connect(updates.append)
    worker.patient_finished.connect(checkpoints.append)

    class Service:
        def search_variants(self, variants, databases, *args, checkpoint, **kwargs):
            results = {}
            for item in variants:
                key = f"{item.sample}|{item.hgvsc}"
                results[key] = [DatabaseEvidence("ClinVar", "not_found", "Synthetic empty result")]
                checkpoint({key: results[key]})
            return results

    monkeypatch.setattr(worker, "_browser_service", Service)
    worker.run()
    qt_app.processEvents()
    assert len(updates) == 2
    assert len(checkpoints) == 1
    assert set(checkpoints[0]) == {"DEMO_VPM_A|c.524G>A", "DEMO_VPM_A|c.743G>A"}


def test_worker_wide_session_failure_restores_controls_and_keeps_prior_result(window):
    from archer_processor.services.browser_sessions import BrowserSessionStatus
    window._set_busy("Checking browser sessions")
    window._session_checked(BrowserSessionStatus("ClinVar", "public", "Synthetic check"))
    window._session_check_failed("Synthetic unexpected failure")
    assert not window._operation_active
    assert window.check_sessions_btn.isEnabled()
    assert window.session_status_labels["ClinVar"].text() == "Offentlig tilgang"
    assert window.session_status_labels["COSMIC"].text() == "Kunne ikke sjekke"


def test_closing_during_running_worker_preserves_application(window):
    from types import SimpleNamespace
    from PyQt6.QtGui import QCloseEvent
    window.session_check_thread = SimpleNamespace(isRunning=lambda: True)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    window.session_check_thread = None
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()


def test_narrow_table_keeps_status_text_readable_by_allowing_horizontal_scroll(window, qt_app):
    item = variant()
    window.result = ProcessingResult(Path("synthetic.tsv"), None, "2026-10-07", [item], [])
    window.db_checks["Franklin"].setChecked(False)
    window._switch_page(1)
    window.resize(1024, 640)
    window.show()
    window._refresh_operations_cockpit()
    qt_app.processEvents()
    column = window.databases.index("Franklin") + 2
    label = window.status_matrix.item(0, column).text()
    assert window.status_matrix.columnWidth(column) >= window.status_matrix.fontMetrics().horizontalAdvance(label) + 24
    assert window.status_matrix.columnWidth(0) >= 160
    assert window.status_matrix.horizontalScrollBar().maximum() > 0
