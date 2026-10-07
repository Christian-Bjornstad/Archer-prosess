from pathlib import Path

from archer_processor.gui.app import MainWindow
from archer_processor.services.settings import AppSettings


def test_reference_edits_are_distinguished_from_saved_settings(qt_app, tmp_path):
    window = MainWindow()
    assert window.reference_selection_status.text() == 'Filvalg er lagret.'
    original = window.settings.who_driver_genes_path
    window.who_genes_edit.setText(str(tmp_path / 'new.xlsx'))
    assert 'Ikke lagret' in window.reference_selection_status.text()
    assert window.settings.who_driver_genes_path == original
    window.who_genes_edit.setText(original)
    assert window.reference_selection_status.text() == 'Filvalg er lagret.'
    window.close()


def test_configuration_save_stays_visible_on_small_screen(qt_app):
    window = MainWindow()
    window._switch_page(2)
    window.resize(1024, 640)
    window.show()
    qt_app.processEvents()
    assert window.settings_sections.count() == 3
    assert [window.settings_sections.tabText(i) for i in range(3)] == ['Referanselister', 'Tilkoblinger', 'Avansert']
    for index in range(3):
        window.settings_sections.setCurrentIndex(index)
        qt_app.processEvents()
        button = window.save_settings_btn
        point = button.mapTo(window, button.rect().bottomRight())
        assert button.isVisible()
        assert point.y() < window.height() - window.status_bar.height()
    window.close()


def test_failed_save_keeps_active_references_and_unsaved_notice(qt_app, tmp_path, monkeypatch):
    window = MainWindow()
    original = window.settings
    window.output_dir_edit.setText(str(tmp_path))
    monkeypatch.setattr('archer_processor.gui.app.QMessageBox.warning', lambda *args: None)
    def fail(_self):
        raise OSError('Synthetic save failure')
    monkeypatch.setattr(AppSettings, 'save', fail)
    assert window._save_settings() is False
    assert window.settings is original
    assert window.settings.default_output_dir != str(tmp_path)
    assert 'Ikke lagret' in window.reference_selection_status.text()
    window.close()


def test_successful_save_updates_reference_notice(qt_app, tmp_path, monkeypatch):
    window = MainWindow()
    window.output_dir_edit.setText(str(tmp_path))
    monkeypatch.setattr(AppSettings, 'save', lambda self: None)
    assert window._save_settings() is True
    assert window.settings.default_output_dir == str(tmp_path)
    assert window.reference_selection_status.text() == 'Filvalg er lagret.'
    window.close()
