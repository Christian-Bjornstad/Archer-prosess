import pytest
from PyQt6.QtWidgets import QPushButton

from archer_processor.core.models import DatabaseEvidence, ProcessingResult, VariantRecord
from archer_processor.gui.app import MainWindow
from archer_processor.gui.widgets.patient_details import PatientDetailsDialog


def loaded_window(tmp_path):
    window = MainWindow()
    variants = [VariantRecord(tmp_path / 'demo.tsv', n, f'DEMO-{n}_VPM_A', 'TP53', f'c.{n}G>A') for n in (1, 2)]
    window.result = ProcessingResult(tmp_path / 'demo.tsv', tmp_path / 'review.xlsx', '2026-10-07', variants, [])
    window._update_evidence_summary()
    return window


def test_main_search_uses_checked_patients_and_focus_does_not_change_scope(qt_app, tmp_path, monkeypatch):
    window = loaded_window(tmp_path)
    calls = []
    monkeypatch.setattr(window, '_start_prioritized_search', lambda: calls.append('selected'))
    monkeypatch.setattr(window, '_start_remaining_search', lambda: calls.append('remaining'))
    window.status_matrix.selectRow(0)
    window._start_primary_search()
    assert calls == ['remaining']
    window.status_matrix.set_selected_patients(['DEMO-1'])
    window._start_primary_search()
    assert calls == ['remaining', 'selected']
    window.close()


def test_report_label_exposes_all_or_checked_patient_scope(qt_app, tmp_path):
    window = loaded_window(tmp_path)
    assert 'alle 2' in window.patient_excel_btn.text()
    window.status_matrix.set_selected_patients(['DEMO-1'])
    assert '1 valgt' in window.patient_excel_btn.text()
    assert window._selected_patient_ids() == ['DEMO-1']
    window.status_matrix.selectRow(1)
    assert window._selected_patient_ids() == ['DEMO-1']
    window.close()


@pytest.mark.parametrize('size', [(920, 600), (1024, 640), (1440, 900)])
def test_compact_controls_and_table_fit_short_workstation(qt_app, size):
    window = MainWindow()
    window.resize(*size)
    window._switch_page(1)
    window.show()
    qt_app.processEvents()
    for control in (window.search_btn, window.status_matrix, window.patient_excel_btn, window.load_selection_btn):
        point = control.mapTo(window, control.rect().bottomRight())
        assert point.y() < window.height() - window.status_bar.height()
        assert point.x() < window.width()
        assert control.isVisible()
    assert window.status_matrix.height() >= 150
    assert window.advanced_evidence_content.isHidden()
    window.close()


def test_only_one_visible_pause_stop_pair(qt_app):
    window = MainWindow()
    window._switch_page(1)
    window.show()
    window._set_busy('Searching')
    qt_app.processEvents()
    visible = [b for b in window.findChildren(QPushButton) if b.isVisible()]
    assert window.pause_search_btn is window.run_status_strip.pause_button
    assert window.stop_search_btn is window.run_status_strip.stop_button
    assert len([b for b in visible if b.text() == 'Pause']) == 1
    assert len([b for b in visible if b.text() == 'Stopp']) == 1
    window._set_ready()
    window.close()


def test_open_patient_details_refresh_with_incoming_evidence(qt_app, tmp_path, monkeypatch):
    window = loaded_window(tmp_path)
    variant = window.result.variants[0]
    window.db_checks['ClinVar'].setChecked(True)
    key = f'{variant.sample}|{variant.hgvsc}'
    window.evidence[key] = [DatabaseEvidence('ClinVar', 'error', 'Synthetic failure')]
    window._update_evidence_summary()
    window.status_matrix.setCurrentCell(0, 2)

    def inspect(dialog):
        assert 'Synthetic failure' in dialog.details.toPlainText()
        assert dialog.retry_button.isEnabled()
        window.evidence[key] = [DatabaseEvidence('ClinVar', 'found', 'Synthetic updated result')]
        window._update_evidence_summary()
        assert 'Synthetic updated result' in dialog.details.toPlainText()
        assert 'Synthetic failure' not in dialog.details.toPlainText()
        assert not dialog.retry_button.isEnabled()
        return 0

    monkeypatch.setattr(PatientDetailsDialog, 'exec', inspect)
    window._show_patient_details('DEMO-1')
    window.close()


def test_patient_details_retry_targets_only_displayed_patient(qt_app, tmp_path, monkeypatch):
    window = loaded_window(tmp_path)
    variant = window.result.variants[0]
    window.db_checks['ClinVar'].setChecked(True)
    window.evidence[f'{variant.sample}|{variant.hgvsc}'] = [DatabaseEvidence('ClinVar', 'error', 'Synthetic failure')]
    window._update_evidence_summary()
    window.status_matrix.set_selected_patients(['DEMO-2'])
    calls = []
    monkeypatch.setattr(window, '_start_database_search', lambda **kwargs: calls.append(kwargs))

    def retry(dialog):
        dialog.retry_button.click()
        return 0

    monkeypatch.setattr(PatientDetailsDialog, 'exec', retry)
    window._show_patient_details('DEMO-1')
    assert calls == [{'patient_ids': {'DEMO-1'}, 'retry_failed_only': True}]
    assert window.status_matrix.selected_patients() == ['DEMO-2']
    window.close()
