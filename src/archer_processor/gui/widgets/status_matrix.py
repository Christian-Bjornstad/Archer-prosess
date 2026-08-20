from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from archer_processor.gui.status_model import CellState, PatientStatusRow


_STATE_COLORS = {
    CellState.COMPLETE: ("#E9F6EF", "#18794E"),
    CellState.REPORT_SAVED: ("#E9F6EF", "#18794E"),
    CellState.RUNNING: ("#E7F4F7", "#087EA4"),
    CellState.RETRY: ("#FFF5D6", "#714600"),
    CellState.SAVE_PENDING: ("#FFF5D6", "#714600"),
    CellState.NOT_FOUND: ("#F1F5F7", "#516875"),
    CellState.STOPPED: ("#F8E8E8", "#B42318"),
    CellState.QUEUED: ("#F1F5F7", "#516875"),
    CellState.SKIPPED: ("#F1F5F7", "#516875"),
    CellState.NOT_READY: ("#F1F5F7", "#516875"),
}


class StatusMatrix(QTableWidget):
    cell_activated = pyqtSignal(str, str)

    def __init__(self, databases: Sequence[str]) -> None:
        self.databases = list(databases)
        super().__init__(0, 3 + len(self.databases))
        self.setObjectName("PatientStatusMatrix")
        self.setHorizontalHeaderLabels(
            ["Patient", "Variants", *self.databases, "Report"]
        )
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.itemActivated.connect(self._emit_activation)

    def set_rows(self, rows: Sequence[PatientStatusRow]) -> None:
        self.setRowCount(0)
        for row_data in rows:
            row = self.rowCount()
            self.insertRow(row)
            patient = QTableWidgetItem(row_data.patient_id)
            patient.setData(Qt.ItemDataRole.UserRole, row_data.patient_id)
            self.setItem(row, 0, patient)
            self.setItem(row, 1, QTableWidgetItem(str(row_data.variant_count)))
            for column, database in enumerate([*self.databases, "Report"], start=2):
                cell = row_data.cells[database]
                item = QTableWidgetItem(cell.label)
                item.setToolTip(cell.detail or cell.label)
                item.setData(Qt.ItemDataRole.UserRole, (row_data.patient_id, database))
                background, foreground = _STATE_COLORS[cell.state]
                item.setBackground(QColor(background))
                item.setForeground(QColor(foreground))
                self.setItem(row, column, item)

    def _emit_activation(self, item: QTableWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, tuple) and len(value) == 2:
            self.cell_activated.emit(str(value[0]), str(value[1]))
