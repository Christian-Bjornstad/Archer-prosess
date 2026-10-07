from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtWidgets import QHeaderView

from archer_processor.gui.status_model import CellState, PatientStatusRow, StatusCell
from archer_processor.gui.widgets.status_matrix import StatusMatrix


def patient_row(patient_id, *, state=CellState.QUEUED, detail="Awaiting lookup"):
    return PatientStatusRow(
        patient_id,
        2,
        {
            "ClinVar": StatusCell(state, state.value.title(), detail),
            "Franklin": StatusCell(CellState.QUEUED, "Queued"),
            "Report": StatusCell(CellState.NOT_READY, "Not ready"),
        },
    )


def selected_patients(matrix):
    return [
        matrix.item(index.row(), 0).text()
        for index in sorted(matrix.selectionModel().selectedRows(), key=lambda item: item.row())
    ]


def test_provider_update_keeps_current_cell_selection_and_existing_items(qt_app):
    matrix = StatusMatrix(["ClinVar", "Franklin"])
    rows = [patient_row(patient_id) for patient_id in ("A", "B", "C")]
    matrix.set_rows(rows)
    matrix.selectRow(1)
    matrix.selectionModel().select(
        matrix.model().index(2, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    matrix.setCurrentCell(2, 3, QItemSelectionModel.SelectionFlag.NoUpdate)
    patient_item = matrix.item(1, 0)
    changed_item = matrix.item(1, 2)
    unchanged_item = matrix.item(0, 2)
    selection_events = []
    matrix.itemSelectionChanged.connect(lambda: selection_events.append(True))

    matrix.set_rows([rows[0], patient_row("B", state=CellState.COMPLETE, detail="Matched"), rows[2]])

    assert matrix.currentRow() == 2
    assert matrix.currentColumn() == 3
    assert selected_patients(matrix) == ["B", "C"]
    assert matrix.item(1, 0) is patient_item
    assert matrix.item(1, 2) is changed_item
    assert matrix.item(0, 2) is unchanged_item
    assert changed_item.text() == "Complete"
    assert changed_item.toolTip() == "Matched"
    assert selection_events == []


def test_patient_reorder_preserves_selection_and_current_cell_by_patient(qt_app):
    matrix = StatusMatrix(["ClinVar", "Franklin"])
    matrix.set_rows([patient_row(patient_id) for patient_id in ("A", "B", "C")])
    matrix.selectRow(1)
    matrix.selectionModel().select(
        matrix.model().index(2, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    matrix.setCurrentCell(2, 2, QItemSelectionModel.SelectionFlag.NoUpdate)

    matrix.set_rows([patient_row(patient_id) for patient_id in ("C", "D", "B")])

    assert selected_patients(matrix) == ["C", "B"]
    assert matrix.currentRow() == 0
    assert matrix.currentColumn() == 2
    assert matrix.currentItem().data(Qt.ItemDataRole.UserRole) == ("C", "ClinVar")


def test_provider_update_preserves_both_scroll_positions(qt_app):
    matrix = StatusMatrix(["ClinVar", "Franklin"])
    matrix.resize(340, 180)
    matrix.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    for column in range(matrix.columnCount()):
        matrix.setColumnWidth(column, 180)
    rows = [patient_row(f"PATIENT-{index:02d}") for index in range(30)]
    matrix.set_rows(rows)
    matrix.show()
    qt_app.processEvents()
    matrix.verticalScrollBar().setValue(12)
    matrix.horizontalScrollBar().setValue(2)
    vertical_position = matrix.verticalScrollBar().value()
    horizontal_position = matrix.horizontalScrollBar().value()
    assert vertical_position > 0
    assert horizontal_position > 0

    rows[0] = patient_row("PATIENT-00", state=CellState.RUNNING)
    matrix.set_rows(rows)
    qt_app.processEvents()

    assert matrix.verticalScrollBar().value() == vertical_position
    assert matrix.horizontalScrollBar().value() == horizontal_position
    matrix.close()
