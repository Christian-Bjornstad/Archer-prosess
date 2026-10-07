from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
import pytest

from archer_processor.gui.status_model import CellState, PatientStatusRow, StatusCell
from archer_processor.gui.widgets.status_matrix import StatusMatrix


def patient_row(patient_id, *, state=CellState.QUEUED, label="Queued", detail="Awaiting lookup"):
    return PatientStatusRow(
        patient_id,
        2,
        {
            "ClinVar": StatusCell(state, label, detail),
            "Report": StatusCell(CellState.NOT_READY, "Not ready"),
        },
    )


def matrix_with_patients():
    matrix = StatusMatrix(["ClinVar"])
    matrix.set_rows([patient_row(patient_id) for patient_id in ("A", "B", "C")])
    return matrix


def test_focusing_a_patient_does_not_select_it_for_batch_actions(qt_app):
    matrix = matrix_with_patients()

    matrix.selectRow(1)

    assert matrix.selected_patients() == []
    assert matrix.focused_patient() == "B"
    assert matrix.patient_details().patient_id == "B"


def test_checkbox_click_selects_patient_independently_of_focused_row(qt_app):
    matrix = matrix_with_patients()
    matrix.resize(600, 220)
    matrix.show()
    qt_app.processEvents()
    selection_events = []
    matrix.patient_selection_changed.connect(lambda: selection_events.append(matrix.selected_patients()))
    rectangle = matrix.visualItemRect(matrix.item(0, 0))

    QTest.mouseClick(matrix.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(rectangle.left() + 10, rectangle.center().y()))
    matrix.selectRow(2)

    assert matrix.selected_patients() == ["A"]
    assert matrix.focused_patient() == "C"
    assert selection_events == [["A"]]
    matrix.close()


def test_space_toggles_patient_checkbox_from_the_keyboard(qt_app):
    matrix = matrix_with_patients()
    matrix.show()
    matrix.setCurrentCell(1, 0)
    matrix.setFocus()
    qt_app.processEvents()

    QTest.keyClick(matrix, Qt.Key.Key_Space)

    assert matrix.selected_patients() == ["B"]
    QTest.keyClick(matrix, Qt.Key.Key_Space)
    assert matrix.selected_patients() == []
    matrix.close()


def test_select_all_and_unselect_all_emit_once_and_preserve_focus(qt_app):
    matrix = matrix_with_patients()
    matrix.setCurrentCell(1, 2)
    selection_events = []
    matrix.patient_selection_changed.connect(lambda: selection_events.append(matrix.selected_patients()))

    matrix.select_all_patients()
    matrix.select_all_patients()
    matrix.unselect_all_patients()
    matrix.unselect_all_patients()

    assert selection_events == [["A", "B", "C"], []]
    assert matrix.focused_patient() == "B"
    assert matrix.currentColumn() == 2


def test_refresh_preserves_checked_ids_after_patient_reordering(qt_app):
    matrix = matrix_with_patients()
    matrix.set_selected_patients(["B", "C", "UNKNOWN"])
    matrix.setCurrentCell(2, 2)
    selection_events = []
    matrix.patient_selection_changed.connect(lambda: selection_events.append(True))

    matrix.set_rows([patient_row(patient_id) for patient_id in ("C", "D", "B")])

    assert matrix.selected_patients() == ["C", "B"]
    assert matrix.item(0, 0).checkState() == Qt.CheckState.Checked
    assert matrix.item(1, 0).checkState() == Qt.CheckState.Unchecked
    assert matrix.item(2, 0).checkState() == Qt.CheckState.Checked
    assert matrix.focused_patient() == "C"
    assert matrix.currentColumn() == 2
    assert selection_events == []


def test_removing_checked_patient_updates_selection_once(qt_app):
    matrix = matrix_with_patients()
    matrix.set_selected_patients(["A", "B"])
    selection_events = []
    matrix.patient_selection_changed.connect(lambda: selection_events.append(matrix.selected_patients()))

    matrix.set_rows([patient_row("B"), patient_row("C")])

    assert selection_events == [["B"]]
    assert matrix.selected_patients() == ["B"]


def test_status_update_refreshes_focused_details_without_changing_selection(qt_app):
    matrix = matrix_with_patients()
    matrix.set_selected_patients(["A"])
    matrix.setCurrentCell(1, 2)
    focused_events, detail_events, selection_events = [], [], []
    matrix.patient_focused.connect(focused_events.append)
    matrix.patient_details_changed.connect(detail_events.append)
    matrix.patient_selection_changed.connect(lambda: selection_events.append(True))

    matrix.set_rows([
        patient_row("A"),
        patient_row("B", state=CellState.COMPLETE, label="Complete", detail="Exact variant matched"),
        patient_row("C"),
    ])

    assert matrix.selected_patients() == ["A"]
    assert matrix.focused_patient() == "B"
    assert matrix.patient_details().cells["ClinVar"].detail == "Exact variant matched"
    assert matrix.item(1, 2).text() == "Utført"
    assert focused_events == []
    assert detail_events == ["B"]
    assert selection_events == []


def test_focus_signal_changes_only_when_patient_changes_and_clears_when_removed(qt_app):
    matrix = matrix_with_patients()
    focused_events = []
    matrix.patient_focused.connect(focused_events.append)

    matrix.setCurrentCell(1, 0)
    matrix.setCurrentCell(1, 2)
    matrix.setCurrentCell(2, 2)
    matrix.set_rows([patient_row("A"), patient_row("B")])

    assert focused_events == ["B", "C", ""]
    assert matrix.focused_patient() is None
    assert matrix.patient_details() is None


def test_patient_details_return_a_snapshot_without_mutating_clinical_states(qt_app):
    matrix = matrix_with_patients()
    details = matrix.patient_details("A")
    details.cells.clear()

    assert matrix.patient_details("A").cells["ClinVar"].state is CellState.QUEUED
    assert matrix.patient_details("A").cells["ClinVar"].label == "Queued"
    assert matrix.patient_details("UNKNOWN") is None


@pytest.mark.parametrize("label, display", [
    ("Running", "Søker"), ("Queued", "Venter"), ("Retry", "Prøv igjen"),
    ("Error", "Feil"), ("Not found", "Ingen treff"), ("Complete", "Utført"),
    ("Not selected", "Ikke valgt"), ("Skipped", "Hoppet over"),
])
def test_norwegian_display_preserves_counts_and_original_status_labels(qt_app, label, display):
    matrix = StatusMatrix(["ClinVar"])
    row = patient_row("A", state=CellState.RETRY, label=f"{label} (1/3)")

    matrix.set_rows([row])

    assert matrix.item(0, 2).text() == f"{display} (1/3)"
    assert row.cells["ClinVar"].label == f"{label} (1/3)"
    assert matrix.patient_details("A").cells["ClinVar"].state is CellState.RETRY


def test_source_activation_keeps_existing_patient_and_database_contract(qt_app):
    matrix = matrix_with_patients()
    activations = []
    matrix.cell_activated.connect(lambda patient, database: activations.append((patient, database)))

    matrix.itemActivated.emit(matrix.item(1, 2))

    assert activations == [("B", "ClinVar")]
    assert matrix.item(1, 1).text() == "2"


def test_compact_column_headers_keep_provider_offsets(qt_app):
    matrix = matrix_with_patients()

    assert [matrix.horizontalHeaderItem(column).text() for column in range(4)] == [
        "Pasient", "Varianter", "ClinVar", "Vedlegg",
    ]
    assert matrix.columnWidth(0) == 160
    assert matrix.columnWidth(1) == 80
    assert matrix.columnWidth(2) == 128
    assert matrix.columnWidth(3) >= 132
