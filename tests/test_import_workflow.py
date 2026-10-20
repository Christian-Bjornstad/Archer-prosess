from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QDate
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox

from archer_processor.core.models import ProcessingResult, VariantRecord
from archer_processor.gui.app import MainWindow
from archer_processor.services.settings import AppSettings


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(AppSettings, "load", classmethod(
        lambda cls: cls(default_output_dir=str(tmp_path), offer_recent_analysis=False)
    ))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    value = MainWindow()
    yield value
    value._set_ready()
    value.close()


def assert_visible_in_import(window, control):
    viewport = window.import_scroll.viewport()
    point = control.mapTo(viewport, control.rect().topLeft())
    assert control.isVisible()
    assert point.x() >= 0
    assert point.y() >= 0
    assert point.x() + control.width() <= viewport.width()
    assert point.y() + control.height() <= viewport.height()


def synthetic_result(tmp_path):
    tsv = tmp_path / "synthetic.tsv"
    tsv.write_text("synthetic-only", encoding="utf-8")
    workbook = tmp_path / "synthetic_VPM_review.xlsx"
    workbook.touch()
    item = VariantRecord(tsv, 2, "DEMO_VPM_A", "TP53", "c.524G>A")
    return ProcessingResult(tsv, workbook, "2026-10-07", [item], [])


def test_both_import_paths_and_primary_action_fit_short_workstation(window, qt_app):
    window.resize(1024, 640)
    window.show()
    qt_app.processEvents()
    for control in (window.new_analysis_btn, window.resume_analysis_btn,
                    window.input_edit, window.output_edit, window.process_btn):
        assert_visible_in_import(window, control)
    window.resume_analysis_btn.click()
    qt_app.processEvents()
    for control in (window.new_analysis_btn, window.resume_analysis_btn, window.resume_btn):
        assert_visible_in_import(window, control)
    assert not window.input_edit.isVisible()


def test_switching_import_paths_preserves_draft_and_does_not_start_work(window, tmp_path):
    result = synthetic_result(tmp_path)
    window._open_analysis_file(str(result.input_path))
    window.run_date.setDate(QDate(2026, 10, 6))
    window.hide_excluded.setChecked(True)
    draft = window.input_edit.text(), window.output_edit.text()
    window.resume_analysis_btn.click()
    window.new_analysis_btn.click()
    assert (window.input_edit.text(), window.output_edit.text()) == draft
    assert window.run_date.date() == QDate(2026, 10, 6)
    assert window.hide_excluded.isChecked()
    assert window.processing_thread is None
    assert window.workbook_load_thread is None
    assert window.import_modes.currentIndex() == 0


def test_dropped_tsv_returns_to_new_analysis_and_keeps_path_autodetection(window, tmp_path):
    folder = tmp_path / "2026_09_30_VPM"
    folder.mkdir()
    tsv = folder / "variants.tsv"
    tsv.touch()
    window.resume_analysis_btn.click()
    window.file_drop.file_selected.emit(str(tsv))
    assert window.import_modes.currentIndex() == 0
    assert window.new_analysis_btn.isChecked()
    assert window.input_edit.text() == str(tsv)
    assert Path(window.output_edit.text()) == tmp_path / "variants_VPM_review.xlsx"
    assert window.run_date.date() == QDate(2026, 9, 30)
    assert not window.run_date.isEnabled()
    assert window.process_btn.isEnabled()


def test_created_workbook_advances_open_then_evidence_without_silent_recreation(
    window, monkeypatch, tmp_path,
):
    result = synthetic_result(tmp_path)
    window._open_analysis_file(str(result.input_path))
    window._processing_finished(result)
    assert not window.process_btn.isEnabled()
    assert window.open_workbook_btn.objectName() == "PrimaryButton"
    assert "X" in window.import_guidance.text()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    window.open_workbook_btn.click()
    assert len(opened) == 1
    assert Path(opened[0].toLocalFile()) == result.output_path.resolve()
    assert window.continue_evidence_btn.objectName() == "PrimaryButton"
    assert "Lagre" in window.import_guidance.text()
    assert "Last inn" in window.import_guidance.text()
    assert window.database_skip_keys == set()
    window._set_ready()
    assert not window.process_btn.isEnabled()
    window.continue_evidence_btn.click()
    assert window.tabs.currentIndex() == 1
    assert window.result is result


def test_failed_excel_launch_keeps_open_as_next_step(window, monkeypatch, tmp_path):
    result = synthetic_result(tmp_path)
    window._open_analysis_file(str(result.input_path))
    window._processing_finished(result)
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: False)
    window.open_workbook_btn.click()
    assert window.open_workbook_btn.objectName() == "PrimaryButton"
    assert window.continue_evidence_btn.objectName() != "PrimaryButton"
    assert not window.process_btn.isEnabled()


def test_new_draft_restores_create_action_after_generated_workbook(window, tmp_path):
    result = synthetic_result(tmp_path)
    window._open_analysis_file(str(result.input_path))
    window._processing_finished(result)
    changed = tmp_path / "other-synthetic.tsv"
    changed.touch()
    window._open_analysis_file(str(changed))
    assert window.process_btn.isEnabled()
    assert window.process_btn.objectName() == "PrimaryButton"
    assert not window.open_workbook_btn.isVisibleTo(window.import_scroll.widget())
    assert not window.continue_evidence_btn.isVisibleTo(window.import_scroll.widget())


def test_restored_analysis_promotes_search_and_preserves_loaded_x_selections(window, tmp_path):
    result = synthetic_result(tmp_path)
    state = SimpleNamespace(result=result, evidence={}, database_skip_keys={"DEMO_VPM_A|c.524G>A"})
    window._processed_workbook_loaded(result.output_path, state)
    assert window.import_modes.currentIndex() == 1
    assert window.continue_evidence_btn.objectName() == "PrimaryButton"
    assert window.continue_evidence_btn.isEnabled()
    assert not window.process_btn.isEnabled()
    assert window.database_skip_keys == state.database_skip_keys
    assert "1" in window.resume_status.text()


def test_import_next_step_actions_cannot_run_while_analysis_is_owned(window, tmp_path):
    result = synthetic_result(tmp_path)
    window._open_analysis_file(str(result.input_path))
    window._processing_finished(result)
    window._set_busy("Searching")
    for control in (window.new_analysis_btn, window.resume_analysis_btn,
                    window.open_workbook_btn, window.continue_evidence_btn, window.process_btn):
        assert not control.isEnabled()
    window._set_ready()
    assert window.open_workbook_btn.isEnabled()
    assert window.continue_evidence_btn.isEnabled()
    assert not window.process_btn.isEnabled()


def test_output_alias_cannot_recreate_the_generated_review(window, tmp_path, monkeypatch):
    result = synthetic_result(tmp_path)
    window._open_analysis_file(str(result.input_path))
    window._processing_finished(result)
    window.output_edit.setText(str(result.output_path.with_suffix('')))
    saves = []
    monkeypatch.setattr(window, '_save_settings', lambda **kwargs: saves.append(True) or False)
    window._start_processing()
    assert saves == []
    assert not window.process_btn.isEnabled()
    assert window.open_workbook_btn.isEnabled()
