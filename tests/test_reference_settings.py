from pathlib import Path

from openpyxl import Workbook
import pytest

from archer_processor.core import default_artifact_rules
from archer_processor.gui.app import MainWindow, ProcessingWorker, ProcessedWorkbookWorker
from archer_processor.services.credentials import CredentialStoreError
from archer_processor.services.settings import AppSettings


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(AppSettings, "load", classmethod(lambda cls: cls(default_output_dir=str(tmp_path))))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    value = MainWindow()
    yield value
    value.close()


def catalog(tmp_path, threshold="5%"):
    book = Workbook()
    sheet = book.active
    sheet.append(["Gene", "HGVSc", "Artifact through AF", "Reason"])
    sheet.append(["FLT3", "NM_004119.2:c.1419-4dup", threshold, "Synthetic override"])
    path = tmp_path / "artifacts.xlsx"
    book.save(path)
    book.close()
    return path


def test_settings_persist_catalog_path_and_keep_manual_fallback(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config))
    monkeypatch.setattr("archer_processor.services.settings.credentials.save_password", lambda *args: None)
    monkeypatch.setattr("archer_processor.services.settings.credentials.get_saved_password", lambda *args: "")
    value = AppSettings(artifact_rules_path="C:/local/artifacts.xlsx")
    value.save()
    loaded = AppSettings.load()
    assert loaded.artifact_rules_path == "C:/local/artifacts.xlsx"
    assert loaded.artifact_rules == default_artifact_rules()


def test_settings_validate_selected_excel_without_overwriting_manual_rules(window, tmp_path):
    path = catalog(tmp_path)
    window.artifact_path_edit.setText(str(path))
    assert window._validate_artifact_path()
    assert "1 regler" in window.artifact_path_status.text()
    assert not window.artifact_table.isEnabled()
    assert window._save_settings(silent=True)
    assert window.settings.artifact_rules_path == str(path.resolve())
    assert window.settings.artifact_rules == default_artifact_rules()
    window.artifact_path_edit.clear()
    assert window._validate_artifact_path()
    assert window.artifact_table.isEnabled()
    assert window._save_settings(silent=True)
    assert window.settings.artifact_rules_path == ""


def test_invalid_catalog_prevents_saving_and_keeps_previous_source(window, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr("archer_processor.gui.app.QMessageBox.warning", lambda *args: warnings.append(args))
    window.artifact_path_edit.setText(str(tmp_path / "missing.xlsx"))
    assert not window._validate_artifact_path()
    assert not window._save_settings(silent=True)
    assert window.settings.artifact_rules_path == ""
    assert "finnes ikke" in warnings[0][2]
    assert window.tabs.currentIndex() == 2


def test_processing_and_resume_load_external_rules_afresh(tmp_path, qt_app):
    path = catalog(tmp_path)
    settings = AppSettings(artifact_rules_path=str(path))
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    review = tmp_path / "synthetic-review.xlsx"
    processed, errors = [], []
    worker = ProcessingWorker(fixture, review, "2026-10-07", settings, False)
    worker.finished.connect(processed.append)
    worker.failed.connect(errors.append)
    worker.run()
    assert not errors
    assert processed[0].variants[0].decision == "included"
    catalog(tmp_path, threshold=None)
    restored = []
    resume = ProcessedWorkbookWorker(review, settings)
    resume.finished.connect(lambda path, state: restored.append(state))
    resume.failed.connect(errors.append)
    resume.run()
    assert not errors
    assert restored[0].result.variants[0].decision == "excluded"
    assert restored[0].result.variants[0].decision_reason == "Synthetic override"


def test_bad_external_catalog_stops_processing_before_output(tmp_path, qt_app):
    settings = AppSettings(artifact_rules_path=str(tmp_path / "missing.xlsx"))
    output = tmp_path / "should-not-exist.xlsx"
    worker = ProcessingWorker(Path("missing-input.tsv"), output, "2026-10-07", settings, False)
    errors = []
    worker.failed.connect(errors.append)
    worker.run()
    assert len(errors) == 1
    assert "Artefaktlisten finnes ikke" in errors[0]
    assert not output.exists()


def test_credential_storage_failure_returns_to_settings_without_raising(window, monkeypatch):
    warnings = []
    monkeypatch.setattr("archer_processor.gui.app.QMessageBox.warning", lambda *args: warnings.append(args))
    def fail_save(self):
        raise CredentialStoreError("Synthetic unavailable credential store")
    monkeypatch.setattr(AppSettings, "save", fail_save)
    assert not window._save_settings(silent=True)
    assert "credential store" in warnings[0][2]


def test_reference_file_removed_before_priority_reports_releases_gui_and_keeps_queue(window, tmp_path, monkeypatch):
    from archer_processor.core import VariantProcessor
    warnings = []
    monkeypatch.setattr("archer_processor.gui.app.QMessageBox.warning", lambda *args: warnings.append(args))
    window.result = VariantProcessor().process(Path(__file__).parent / "fixtures" / "sample_variants.tsv", "2026-10-07", tmp_path / "review.xlsx")
    patient_ids = [window.result.variants[0].patient_id]
    window.settings.who_driver_genes_path = str(tmp_path / "removed.xlsx")
    window._set_busy("Searching")
    window._start_patient_reports(patient_ids)
    assert not window._operation_active
    assert window.patient_report_thread is None
    assert window._pending_report_after_workbook == patient_ids
    assert "finnes ikke" in warnings[0][2]


@pytest.mark.parametrize("error", [OSError("Synthetic disk failure"), CredentialStoreError("Synthetic credential failure")])
def test_remembering_recent_workbook_is_nonessential_to_loaded_analysis(window, tmp_path, monkeypatch, error):
    def fail_save(self):
        raise error
    monkeypatch.setattr(AppSettings, "save", fail_save)
    window._remember_recent_workbook(tmp_path / "synthetic.xlsx")
    assert "Kunne ikke lagre nylig analyse" in window.log.toPlainText()
