from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QMessageBox

from archer_processor.core import VariantProcessor
from archer_processor.gui.app import BrowserReviewWorker, MainWindow
from archer_processor.reports import ExcelReportWriter, PatientReportOutcome
from archer_processor.services.settings import AppSettings


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(AppSettings, "load", classmethod(lambda cls: cls(default_output_dir=str(tmp_path))))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *args: None)
    value = MainWindow()
    yield value
    value.close()


def pump(qt_app, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert predicate()
    for _ in range(5):
        qt_app.processEvents()
        time.sleep(0.005)


def processed(tmp_path, filename="review.xlsx"):
    return VariantProcessor().process(
        Path(__file__).parent / "fixtures" / "sample_variants.tsv",
        "2026-10-07", tmp_path / filename,
    )


def test_processed_workbooks_can_be_resumed_repeatedly(window, qt_app, tmp_path):
    first, second = processed(tmp_path, "first.xlsx"), processed(tmp_path, "second.xlsx")
    ExcelReportWriter().write(first, first.output_path)
    ExcelReportWriter().write(second, second.output_path)

    window._load_processed_workbook(first.output_path)
    pump(qt_app, lambda: not window._operation_active)
    window._load_processed_workbook(second.output_path)
    pump(qt_app, lambda: not window._operation_active)

    assert window.result.output_path == second.output_path
    assert window.workbook_load_thread is None
    assert window.workbook_load_worker is None


@pytest.mark.parametrize("action", ["stop", "pause"])
def test_old_deleted_search_thread_does_not_block_current_browser_controls(
    window, tmp_path, action,
):
    old = QThread()
    sip.delete(old)
    window.database_thread = old
    worker = BrowserReviewWorker([], [], tmp_path, window.settings)
    interruptions = []
    window.browser_thread = SimpleNamespace(
        isRunning=lambda: True, requestInterruption=lambda: interruptions.append(True),
    )
    window.browser_worker = worker
    window._set_busy("Browser lookups")
    try:
        if action == "stop":
            window._stop_evidence_search()
            assert interruptions == [True]
            assert window._search_stop_requested
        else:
            window._toggle_search_pause()
            assert worker.pause_control.pause_requested
            assert window._search_pause_requested
    finally:
        window.database_thread = None
        window.browser_thread = None
        window.browser_worker = None


def test_finished_session_worker_releases_owned_qt_references(window, qt_app, monkeypatch):
    from archer_processor.services import browser_sessions

    monkeypatch.setattr(browser_sessions.BrowserSessionCheckService, "check_all", lambda self, **kwargs: [])
    window._start_session_check()
    pump(qt_app, lambda: not window._operation_active)

    assert window.session_check_thread is None
    assert window.session_check_worker is None


def test_pending_priority_checkpoint_cannot_switch_analysis_or_start_session(
    window, qt_app, tmp_path, monkeypatch,
):
    from archer_processor.reports import PatientReportCoordinator

    first, second = processed(tmp_path, "first.xlsx"), processed(tmp_path, "second.xlsx")
    window.result = first
    patient = first.variants[0].patient_id
    window._active_search_report_patient_ids = [patient]
    entered, release = threading.Event(), threading.Event()

    def slow_write(self, result, path, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return path

    monkeypatch.setattr(ExcelReportWriter, "write", slow_write)
    monkeypatch.setattr(PatientReportCoordinator, "write_patient", lambda self, patient_id: PatientReportOutcome(
        patient_id, self.result.output_path.parent / "synthetic-report.xlsx", "created", "Synthetic report",
    ))
    window._set_busy("Searching")
    window._database_finished({})
    try:
        pump(qt_app, entered.is_set)
        assert window._operation_active
        assert not window.resume_btn.isEnabled()
        assert not window.check_sessions_btn.isEnabled()
        assert not window.stop_search_btn.isEnabled()
        window._load_processed_workbook(second.output_path)
        window._start_session_check()
        assert window.workbook_load_thread is None
        assert window.session_check_thread is None
        assert window.result is first
    finally:
        release.set()
        pump(qt_app, lambda: window.workbook_write_thread is None and not window._operation_active)

    assert patient in window.report_outcomes
    assert window.result is first


def test_failed_final_checkpoint_releases_ui_without_running_priority_reports(
    window, qt_app, tmp_path, monkeypatch,
):
    window.result = processed(tmp_path)
    patient = window.result.variants[0].patient_id
    window._active_search_report_patient_ids = [patient]
    monkeypatch.setattr(ExcelReportWriter, "write", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("Synthetic Excel lock")))
    window._set_busy("Searching")
    window._database_finished({})
    pump(qt_app, lambda: window.workbook_write_thread is None and not window._operation_active)

    assert window.workbook_write_pending
    assert window._pending_report_after_workbook == [patient]
    assert window.report_outcomes == {}
    assert window.rewrite_btn.isEnabled()


def test_old_finished_thread_cannot_clear_a_new_browser_owner(window, qt_app, monkeypatch):
    from archer_processor.services.browser_review import BrowserReviewService

    entered, release = threading.Event(), threading.Event()

    def login(self, provider):
        entered.set()
        assert release.wait(5)
        return "Synthetic login finished"

    monkeypatch.setattr(BrowserReviewService, "open_login", login)
    window._start_browser_login()
    pump(qt_app, entered.is_set)
    old_thread = window.browser_thread
    replacement_thread, replacement_worker = QThread(), object()
    window.browser_thread, window.browser_worker = replacement_thread, replacement_worker
    try:
        release.set()
        pump(qt_app, lambda: not window._operation_active)
        assert not window._thread_is_running(old_thread)
        assert window.browser_thread is replacement_thread
        assert window.browser_worker is replacement_worker
    finally:
        window.browser_thread = None
        window.browser_worker = None


def test_final_checkpoint_keeps_completion_visible_after_save(
    window, qt_app, tmp_path, monkeypatch,
):
    window.result = processed(tmp_path)
    monkeypatch.setattr(ExcelReportWriter, "write", lambda self, result, path, *args, **kwargs: path)
    window._set_busy("Searching")
    window._database_finished({})
    pump(qt_app, lambda: window.workbook_write_thread is None and not window._operation_active)

    assert window.status_badge.text() == "Search complete"
    assert window.run_status_strip.phase_label.text() == "Search complete"


def test_starting_another_search_does_not_release_stale_pending_reports(
    window, tmp_path, monkeypatch,
):
    window.result = processed(tmp_path)
    window._pending_report_after_workbook = [window.result.variants[0].patient_id]
    window._prepare_search_status(window.result.variants, ["ClinVar"], set())
    window._set_busy("Searching")
    window._background_workbook_path = window.result.output_path
    monkeypatch.setattr(window, "_start_patient_reports", lambda patient_ids: pytest.fail("An intermediate checkpoint must not start the previous run's reports"))

    window._background_workbook_thread_finished()

    assert window._operation_active
    assert window._pending_report_after_workbook == []
